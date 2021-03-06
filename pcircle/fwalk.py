#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import print_function

# Disable auto init
# from mpi4py import rc
# rc.initialize = False
from mpi4py import MPI

from scandir import scandir
import stat
import os
import os.path
import sys
import argparse
import xattr
import numpy as np

from task import BaseTask
from circle import Circle
from globals import G
from utils import getLogger, bytes_fmt, destpath
from dbstore import DbStore
from fdef import FileItem
from mpihelper import ThrowingArgumentParser, tally_hosts, parse_and_bcast

import utils

from _version import get_versions
__version__ = get_versions()['version']
args = None
log = getLogger(__name__)
taskloads = []
bins = None
comm = MPI.COMM_WORLD


def err_and_exit(msg, code=0):
    if comm.rank == 0:
        print("\n%s" % msg)
    MPI.Finalize()
    sys.exit(0)


def gen_parser():
    parser = ThrowingArgumentParser(description="fwalk")
    parser.add_argument("-v", "--version", action="version", version="{version}".format(version=__version__))
    parser.add_argument("--loglevel", default="ERROR", help="log level")
    parser.add_argument("path", nargs='+', default=".", help="path")
    parser.add_argument("-i", "--interval", type=int, default=10, help="interval")
    parser.add_argument("--use-store", action="store_true", help="Use persistent store")
    parser.add_argument("-s", "--stats", action="store_true", help="collects stats")
    parser.add_argument("-t", "--top", type=int, default=10, help="Top files (10)")

    return parser


def local_histogram(flist):
    """ A list FileItems, build np array  """
    global bins
    b4k = 4 * 1024
    b64k = 64 * 1024
    b512k = 512 * 1024
    b1m = 1024 *1024
    b4m = 4 * b1m
    b16m = 16 * b1m
    b512m = 512 * b1m
    b1g = 2 * b512m
    b100g = 100 * b1g
    b512g = 512 * b1g
    b1tb = 1024 * b1g
    bins = [ 0, b4k, b64k,b512k, b1m, b4m, b16m, b512m, b1g, b512g, b1tb]
    fsizes = [f.st_size for f in flist]
    arr = np.array(fsizes)
    hist, _ = np.histogram(arr, bins)
    return hist


def global_histogram(treewalk):
    all_hist = None
    local_hist = local_histogram(treewalk.flist)
    all_hist = comm.gather(local_hist)
    if comm.rank == 0:
        local_hist = sum(all_hist)
    return local_hist


class FWalk(BaseTask):

    def __init__(self, circle, src, dest=None, preserve=False, force=False):
        BaseTask.__init__(self, circle)

        self.d = {"rank": "rank %s" % circle.rank}
        self.circle = circle
        self.src = src
        self.dest = dest
        self.force = force
        self.interval = 10  # progress report

        # For now, I am setting the option
        # TODO: should user allowed to meddle this?
        self.sizeonly = False
        self.checksum = False

        # to be fixed
        self.optlist = []  # files
        self.opt_dir_list = []  # dirs

        self.sym_links = 0
        self.follow_sym_links = False

        self.workdir = os.getcwd()
        self.tempdir = os.path.join(self.workdir, ".pcircle")
        if not os.path.exists(self.tempdir):
            os.mkdir(self.tempdir)

        if G.use_store:
            self.dbname = "%s/fwalk.%s" % (self.tempdir, circle.rank)
            self.flist = DbStore(self.dbname)
            self.flist_buf = []
        else:
            self.flist = []
        self.src_flist = self.flist

        # hold unlinkable dest directories
        # we have to do the --fix-opt at the end
        self.dest_dirs = []

        self.cnt_dirs = 0
        self.cnt_files = 0
        self.cnt_filesize = 0
        self.last_cnt = 0
        self.skipped = 0
        self.last_reduce_time = MPI.Wtime()

        # reduce
        self.reduce_items = 0

        self.time_started = MPI.Wtime()
        self.time_ended = None

    def create(self):
        if self.circle.rank == 0:
            for ele in self.src:
                self.circle.enq(ele)
            print("\nAnalyzing workload ...")

    def copy_xattr(self, src, dest):
        attrs = xattr.listxattr(src)
        for k in attrs:
            try:
                val = xattr.getxattr(src, k)
                xattr.setxattr(dest, k, val)
            except IOError as e:
                log.warn(e, extra=self.d)

    def flushdb(self):
        if len(self.flist_buf) != 0:
            self.flist.mput(self.flist_buf)

    def process_dir(self, fitem, st):
        """ i_dir should be absolute path
        st is the stat object associated with the directory
        """
        i_dir = fitem.path

        if self.dest:
            # we create destination directory
            # but first we check if we need to change mode for it to work
            o_dir = destpath(fitem, self.dest)
            mode = st.st_mode
            if not (st.st_mode & stat.S_IWUSR):
                mode = st.st_mode | stat.S_IWUSR
                self.opt_dir_list.append((o_dir, st))
            try:
                os.mkdir(o_dir, mode)
            except OSError as e:
                log.debug("mkdir(): %s" % e, extra=self.d)

            if G.preserve:
                self.copy_xattr(i_dir, o_dir)

        last_report = MPI.Wtime()
        count = 0
        try:
            entries = scandir(i_dir)
        except OSError as e:
            log.warn(e, extra=self.d)
            self.skipped += 1
        else:
            for entry in entries:
                elefi = FileItem(entry.path)
                if fitem.dirname:
                    elefi.dirname = fitem.dirname
                self.circle.enq(elefi)

                count += 1
                if (MPI.Wtime() - last_report) > self.interval:
                    print("Rank %s : Scanning [%s] at %s" % (self.circle.rank, i_dir, count))
                    last_report = MPI.Wtime()
            log.info("Finish scan of [%s], count=%s" % (i_dir, count), extra=self.d)

    def do_metadata_preserve(self, src_file, dest_file, st):
        """ create file node, copy attribute if needed."""
        if sys.platform == "darwin":  # Mac OS mknod() not permitted
            return

        try:
            mode = st.st_mode
            if not (st.st_mode & stat.S_IWUSR):
                # owner can't write, we will change mode first
                # then put it in optlist to fix
                mode = st.st_mode | stat.S_IWUSR
                self.optlist.append((dest_file,st))
            os.mknod(dest_file, mode)  # -r-r-r special
        except OSError as e:
            log.warn("mknod(): for %s, %s" % (dest_file, e), extra=self.d)
            return

        if G.preserve:
            self.copy_xattr(src_file, dest_file)

    def check_dest_exists(self, src_file, dest_file):
        """ return True if dest exists and checksum verified correct
            return False if (1) no overwrite (2) destination doesn't exist
        """
        if not self.force:
            return False

        if not os.path.exists(dest_file):
            return False

        # well, destination exists, now we have to check
        if self.sizeonly:
            if os.path.getsize(src_file) == os.path.getsize(dest_file):
                log.warn("Check sizeonly Okay: src: %s, dest=%s" % (src_file, dest_file),
                                 extra=self.d)
                return True
        elif self.checksum:
            raise NotImplementedError("Checksum comparison")

        try:
            os.unlink(dest_file)
        except OSError as e:
            log.warn("Can't unlink %s" % dest_file, extra=self.d)
        else:
            log.info("Retransfer: %s" % src_file, extra=self.d)

        return False

    def append_fitem(self, fitem):
        if G.use_store:
            self.flist_buf.append(fitem)
            if len(self.flist_buf) == G.DB_BUFSIZE:
                self.flist.mput(self.flist_buf)
                del self.flist_buf[:]

        else:
            self.flist.append(fitem)

    def process(self):
        """ process a work unit, spath, dpath refers to
            source and destination respectively """

        fitem = self.circle.deq()
        spath = fitem.path
        if spath:
            try:
                st = os.lstat(spath)
            except OSError as e:
                log.warn(e, extra=self.d)
                self.skipped += 1
                return False

            fitem.st_mode, fitem.st_size, fitem.st_uid, fitem.st_gid = st.st_mode, st.st_size, st.st_uid, st.st_gid
            self.reduce_items += 1

            if os.path.islink(spath):
                self.append_fitem(fitem)
                self.sym_links += 1
                # if not self.follow_sym_links:
                # NOT TO FOLLOW SYM LINKS SHOULD BE THE DEFAULT
                return

            if stat.S_ISREG(st.st_mode):

                if not self.dest:
                    # fwalk without destination, simply add to process list
                    self.append_fitem(fitem)
                else:
                    # self.dest specified, need to check if it is there
                    dpath = destpath(fitem, self.dest)
                    flag = self.check_dest_exists(spath, dpath)
                    if flag:
                        return
                    else:
                        # if src and dest not the same
                        # including the case dest is not there
                        # then we do the following
                        self.append_fitem(fitem)
                        self.do_metadata_preserve(spath, dpath, st)
                self.cnt_files += 1
                self.cnt_filesize += fitem.st_size

            elif stat.S_ISDIR(st.st_mode):
                self.cnt_dirs += 1
                self.process_dir(fitem, st)
                # END OF if spath

    def tally(self, t):
        """ t is a tuple element of flist """
        if stat.S_ISDIR(t[1]):
            self.cnt_dirs += 1
        elif stat.S_ISREG(t[1]):
            self.cnt_files += 1
            self.cnt_filesize += t[2]

    def summarize(self):
        map(self.tally, self.flist)

    def reduce_init(self, buf):
        buf['cnt_files'] = self.cnt_files
        buf['cnt_dirs'] = self.cnt_dirs
        buf['cnt_filesize'] = self.cnt_filesize
        buf['reduce_items'] = self.reduce_items

    def reduce(self, buf1, buf2):
        buf1['cnt_dirs'] += buf2['cnt_dirs']
        buf1['cnt_files'] += buf2['cnt_files']
        buf1['cnt_filesize'] += buf2['cnt_filesize']
        buf1['reduce_items'] += buf2['reduce_items']
        return buf1

    def reduce_report(self, buf):
        # progress report
        # rate = (buf['cnt_files'] - self.last_cnt)/(MPI.Wtime() - self.last_reduce_time)
        # print("Processed objects: %s, estimated processing rate: %d/s" % (buf['cnt_files'], rate))
        # self.last_cnt = buf['cnt_files']

        rate = (buf['reduce_items'] - self.last_cnt) / (MPI.Wtime() - self.last_reduce_time)
        print("Processed objects: %s, estimated processing rate: %d/s" % (buf['reduce_items'], rate))
        self.last_cnt = buf['reduce_items']
        self.last_reduce_time = MPI.Wtime()

    def reduce_finish(self, buf):
        # get result of reduction
        pass

    def total_tally(self):
        global taskloads
        total_dirs = self.circle.comm.reduce(self.cnt_dirs, op=MPI.SUM)
        total_files = self.circle.comm.reduce(self.cnt_files, op=MPI.SUM)
        total_filesize = self.circle.comm.reduce(self.cnt_filesize, op=MPI.SUM)
        total_symlinks = self.circle.comm.reduce(self.sym_links, op=MPI.SUM)
        total_skipped = self.circle.comm.reduce(self.skipped, op=MPI.SUM)
        taskloads = self.circle.comm.gather(self.reduce_items)
        return total_dirs, total_files, total_filesize, total_symlinks, total_skipped

    def epilogue(self):
        total_dirs, total_files, total_filesize, total_symlinks, total_skipped = self.total_tally()
        self.time_ended = MPI.Wtime()

        if self.circle.rank == 0:
            print("\nFWALK Epilogue:\n")
            print("\t{:<20}{:<20}".format("Directory count:", total_dirs))
            print("\t{:<20}{:<20}".format("Sym Links count:", total_symlinks))
            print("\t{:<20}{:<20}".format("File count:", total_files))
            print("\t{:<20}{:<20}".format("Skipped count:", total_skipped))
            print("\t{:<20}{:<20}".format("Total file size:", bytes_fmt(total_filesize)))
            if total_files != 0:
                print("\t{:<20}{:<20}".format("Avg file size:", bytes_fmt(total_filesize/float(total_files))))
            print("\t{:<20}{:<20}".format("Tree talk time:", utils.conv_time(self.time_ended - self.time_started)))
            print("\tFWALK Loads: %s" % taskloads)
            print("")

        return total_filesize

    def cleanup(self):
        if G.use_store:
            self.flist.cleanup()


def main():
    global comm, args
    args = parse_and_bcast(comm, gen_parser)

    try:
        G.src = utils.check_src(args.path)
    except ValueError as e:
        err_and_exit("Error: %s not accessible" % e)

    G.use_store = args.use_store
    G.loglevel = args.loglevel

    hosts_cnt = tally_hosts()

    if comm.rank == 0:
        print("Running Parameters:\n")
        print("\t{:<20}{:<20}".format("FWALK version:", __version__))
        print("\t{:<20}{:<20}".format("Num of hosts:", hosts_cnt))
        print("\t{:<20}{:<20}".format("Num of processes:", MPI.COMM_WORLD.Get_size()))
        print("\t{:<20}{:<20}".format("Root path:", utils.choplist(G.src)))

    circle = Circle()
    treewalk = FWalk(circle, G.src)
    circle.begin(treewalk)

    if G.use_store:
        treewalk.flushdb()

    if args.stats:
        hist = global_histogram(treewalk)
        total = hist.sum()
        bucket_scale = 0.5
        if comm.rank == 0:
            print("\nFileset histograms:\n")
            for idx, rightbound in enumerate(bins[1:]):
                percent = 100 * hist[idx] / float(total)
                star_count = int(bucket_scale * percent)
                print("\t{:<3}{:<15}{:<8}{:<8}{:<50}".format("< ",
                    utils.bytes_fmt(rightbound), hist[idx],
                    "%0.2f%%" % percent, '∎' * star_count))

    if args.stats:
        treewalk.flist.sort(lambda f1, f2: cmp(f1.st_size, f2.st_size), reverse=True)
        globaltops = comm.gather(treewalk.flist[:args.top])
        if comm.rank == 0:
            globaltops = [item for sublist in globaltops for item in sublist]
            globaltops.sort(lambda f1, f2: cmp(f1.st_size, f2.st_size), reverse=True)
            if len(globaltops) < args.top:
                args.top = len(globaltops)
            print("\nStats, top %s files\n" % args.top)
            for i in xrange(args.top):
                print("\t{:15}{:<30}".format(utils.bytes_fmt(globaltops[i].st_size),
                      globaltops[i].path))

    treewalk.epilogue()
    treewalk.cleanup()
    circle.finalize()


if __name__ == "__main__":
    main()
