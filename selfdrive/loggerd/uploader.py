#!/usr/bin/env python
import os
import time
import stat
import random
import ctypes
import inspect
import requests
import traceback
import threading

from selfdrive.swaglog import cloudlog
from selfdrive.loggerd.config import DONGLE_ID, DONGLE_SECRET, ROOT

from common.api import api_get

def raise_on_thread(t, exctype):
  for ctid, tobj in threading._active.items():
    if tobj is t:
      tid = ctid
      break
  else:
    raise Exception("Could not find thread")

  '''Raises an exception in the threads with id tid'''
  if not inspect.isclass(exctype):
    raise TypeError("Only types can be raised (not instances)")

  res = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(tid),
                                                   ctypes.py_object(exctype))
  if res == 0:
    raise ValueError("invalid thread id")
  elif res != 1:
    # "if it returns a number greater than one, you're in trouble,
    # and you should call it again with exc=NULL to revert the effect"
    ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, 0)
    raise SystemError("PyThreadState_SetAsyncExc failed")

def listdir_with_creation_date(d):
  lst = os.listdir(d)
  for fn in lst:
    try:
      st = os.stat(os.path.join(d, fn))
      ctime = st[stat.ST_CTIME]
      yield (ctime, fn)
    except OSError:
      cloudlog.exception("listdir_with_creation_date: stat failed?")
      yield (None, fn)

def listdir_by_creation_date(d):
  times_and_paths = list(listdir_with_creation_date(d))
  return [path for _, path in sorted(times_and_paths)]

def clear_locks(root):
  for logname in os.listdir(root):
    path = os.path.join(root, logname)
    try:
      for fname in os.listdir(path):
        if fname.endswith(".lock"):
          os.unlink(os.path.join(path, fname))
    except OSError:
      cloudlog.exception("clear_locks failed")


class Uploader(object):
  def __init__(self, dongle_id, dongle_secret, root):
    self.dongle_id = dongle_id
    self.dongle_secret = dongle_secret
    self.root = root

    self.upload_thread = None

    self.last_resp = None
    self.last_exc = None

  def clean_dirs(self):
    try:
      for logname in os.listdir(self.root):
        path = os.path.join(self.root, logname)
        # remove empty directories
        if not os.listdir(path):
          os.rmdir(path)
    except OSError:
      cloudlog.exception("clean_dirs failed")

  def gen_upload_files(self):
    if not os.path.isdir(self.root):
      return
    for logname in listdir_by_creation_date(self.root):
      path = os.path.join(self.root, logname)
      names = os.listdir(path)
      if any(name.endswith(".lock") for name in names):
        continue

      for name in names:
        key = os.path.join(logname, name)
        fn = os.path.join(path, name)

        yield (name, key, fn)

  def next_file_to_upload(self):
    # try to upload log files first
    for name, key, fn in self.gen_upload_files():
      if name in ["rlog", "rlog.bz2"]:
        return (key, fn, 0)

    # then upload camera files no not on wifi
    for name, key, fn in self.gen_upload_files():
      if not name.endswith('.lock') and not name.endswith(".tmp"):
        return (key, fn, 1)

    return None


  def do_upload(self, key, fn):
    try:
      url_resp = api_get("upload_url", timeout=2,
                         id=self.dongle_id, secret=self.dongle_secret,
                         path=key)
      url = url_resp.text
      cloudlog.info({"upload_url", url})

      with open(fn, "rb") as f:
        self.last_resp = requests.put(url, data=f)
    except Exception as e:
      self.last_exc = (e, traceback.format_exc())
      raise

  def normal_upload(self, key, fn):
    self.last_resp = None
    self.last_exc = None

    try:
      self.do_upload(key, fn)
    except Exception:
      pass

    return self.last_resp

  def killable_upload(self, key, fn):
      self.last_resp = None
      self.last_exc = None

      self.upload_thread = threading.Thread(target=lambda: self.do_upload(key, fn))
      self.upload_thread.start()
      self.upload_thread.join()
      self.upload_thread = None

      return self.last_resp

  def abort_upload(self):
    thread = self.upload_thread
    if thread is None:
      return
    if not thread.is_alive():
      return
    raise_on_thread(thread, SystemExit)
    thread.join()

  def upload(self, key, fn):
    # write out the bz2 compress
    if fn.endswith("log"):
      ext = ".bz2"
      cloudlog.info("compressing %r to %r", fn, fn+ext)
      if os.system("nice -n 19 bzip2 -c %s > %s.tmp && mv %s.tmp %s%s && rm %s" % (fn, fn, fn, fn, ext, fn)) != 0:
        cloudlog.exception("upload: bzip2 compression failed")
        return False

      # assuming file is named properly
      key += ext
      fn += ext

    try:
      sz = os.path.getsize(fn)
    except OSError:
      cloudlog.exception("upload: getsize failed")
      return False

    cloudlog.event("upload", key=key, fn=fn, sz=sz)

    cloudlog.info("checking %r with size %r", key, sz)

    if sz == 0:
      # can't upload files of 0 size
      os.unlink(fn) # delete the file
      success = True
    else:
      cloudlog.info("uploading %r", fn)
      # stat = self.killable_upload(key, fn)
      stat = self.normal_upload(key, fn)
      if stat is not None and stat.status_code == 200:
        cloudlog.event("upload_success", key=key, fn=fn, sz=sz)
        os.unlink(fn) # delete the file
        success = True
      else:
        cloudlog.event("upload_failed", stat=stat, exc=self.last_exc, key=key, fn=fn, sz=sz)
        success = False

    self.clean_dirs()

    return success



def uploader_fn(exit_event):
  cloudlog.info("uploader_fn")

  uploader = Uploader(DONGLE_ID, DONGLE_SECRET, ROOT)

  while True:
    backoff = 0.1
    while True:

      if exit_event.is_set():
        return

      d = uploader.next_file_to_upload()
      if d is None:
        break

      key, fn, _ = d

      cloudlog.info("to upload %r", d)
      success = uploader.upload(key, fn)
      if success:
        backoff = 0.1
      else:
        cloudlog.info("backoff %r", backoff)
        time.sleep(backoff + random.uniform(0, backoff))
        backoff *= 2
      cloudlog.info("upload done, success=%r", success)

    time.sleep(5)

def main(gctx=None):
  uploader_fn(threading.Event())

if __name__ == "__main__":
  main()

