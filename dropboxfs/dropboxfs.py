import logging
import threading
from contextlib import closing, contextmanager
from datetime import datetime
from io import BytesIO

import dropbox
import six
from dropbox import Dropbox
from dropbox.exceptions import ApiError
from dropbox.files import (DownloadError, FileMetadata, FolderMetadata,
                           LookupError, WriteMode)
from fs import errors
from fs.base import FS
from fs.enums import ResourceType, Seek
from fs.info import Info
from fs.mode import Mode
from fs.subfs import SubFS
from fs.time import datetime_to_epoch, epoch_to_datetime

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)


class DropboxFile(BytesIO):
    def __init__(self, dropbox, path, mode):

        self.dropbox = dropbox
        self.path = path
        self.mode = mode
        self._lock = threading.RLock()

        initialData = None
        self.rev = None
        try:
            metadata, response = self.dropbox.files_download(self.path)
            self.rev = metadata.rev
            with closing(response):

                if self.mode.appending or (
                    self.mode.reading and not self.mode.truncate
                ):
                    initialData = response.content
        except ApiError:

            pass
        super(DropboxFile, self).__init__(initialData)
        if self.mode.appending and initialData is not None:
            # seek to the end
            self.seek(len(initialData))

    if six.PY2:

        def __length_hint__(self):
            return len(self.getvalue())

    else:

        def __length_hint__(self):
            return self.getbuffer().nbytes

    def truncate(self, size=None):

        super(DropboxFile, self).truncate(size)
        data_size = self.__length_hint__()
        if size and data_size < size:
            self.write(b"\0" * (size - data_size))
            self.seek(data_size)
        return size or data_size

    def close(self):

        if not self.mode.writing:
            super(DropboxFile, self).close()
            return
        if self.rev is None:
            writeMode = WriteMode("add")
        else:
            writeMode = WriteMode("update", self.rev)
        metadata = self.dropbox.files_upload(
            self.getvalue(),
            self.path,
            mode=writeMode,
            autorename=False,
            client_modified=datetime.utcnow(),
            mute=False,
        )

        self.path = None
        self.mode = None
        self.dropbox = None
        super(DropboxFile, self).close()

    def write(self, data):
        if self.mode.writing == False:
            raise IOError("File is not in write mode")

        return super(DropboxFile, self).write(data)

    def read(self, size=None):

        if self.mode.reading == False:
            raise IOError("File is not in read mode")

        return super(DropboxFile, self).read(size)

    def readable(self):
        return self.mode.reading

    def writable(self):
        return self.mode.writing


class DropboxFS(FS):
    _meta = {
        "case_insensitive": False,
        "invalid_path_chars": "\0",
        "network": True,
        "read_only": False,
        "thread_safe": True,
        "unicode_paths": True,
        "virtual": False,
    }

    def __init__(self, accessToken, session=None):
        super(DropboxFS, self).__init__()
        self._lock = threading.RLock()
        self.dropbox = Dropbox(accessToken, session=session)

    def fix_path(self, path):

        if isinstance(path, bytes):
            try:
                path = path.decode("utf-8")
            except AttributeError:
                pass
        if not path.startswith("/"):
            path = "/" + path
        if path == "." or path == "./":
            path = "/"
        path = self.validatepath(path)

        return path

    def __repr__(self):
        return "<DropboxDriveFS>"

    def _infoFromMetadata(self, metadata):

        rawInfo = {
            "basic": {
                "name": metadata.name,
                "is_dir": isinstance(metadata, FolderMetadata),
            }
        }
        if isinstance(metadata, FileMetadata):
            rawInfo.update(
                {"details": {"size": metadata.size, "type": ResourceType.file}}
            )
        else:
            rawInfo.update({"details": {"type": ResourceType.directory}})

        return Info(rawInfo)

    def getinfo(self, path, namespaces=None):
        _path = self.fix_path(path)
        if _path == "/":
            info_dict = {
                "basic": {"name": "", "is_dir": True},
                "details": {"type": ResourceType.directory},
            }
            return Info(info_dict)

        try:

            metadata = self.dropbox.files_get_metadata(
                _path, include_media_info=True
            )
        except ApiError as e:
            raise errors.ResourceNotFound(path=path, exc=e)
        return self._infoFromMetadata(metadata)

    def setinfo(self, path, info):
        if not self.exists(path):
            raise errors.ResourceNotFound(path)

    def listdir(self, path):
        _path = self.fix_path(path)

        if _path == "/":
            _path = ""
        if not self.exists(_path):
            raise errors.ResourceNotFound(path)
        meta = self.getinfo(_path)
        if meta.is_file:
            raise errors.DirectoryExpected(path)

        result = self.dropbox.files_list_folder(_path, include_media_info=True)
        allEntries = result.entries
        while result.has_more:
            result = self.dropbox.files_list_folder_continue(result.cursor)
            allEntries += result.entries
        return [x.name for x in allEntries]

    def makedir(self, path, permissions=None, recreate=False):
        path = self.fix_path(path)
        if self.exists(path) and not recreate:
            raise errors.DirectoryExists(path)
        if path == "/":
            return SubFS(self, path)

        if self.exists(path):
            meta = self.getinfo(path)
            if meta.is_dir:
                if recreate == False:
                    raise errors.DirectoryExists(path)
                else:
                    return SubFS(self, path)
            if meta.is_file:
                raise errors.DirectoryExpected(path)

        ppath = self.get_parent(path)
        if not self.exists(ppath):

            raise errors.ResourceNotFound(ppath)

        try:

            folderMetadata = self.dropbox.files_create_folder_v2(path)
        except ApiError as e:

            raise errors.DirectoryExpected(path=path)

        return SubFS(self, path)

    def openbin(self, path, mode="r", buffering=-1, **options):
        path = self.fix_path(path)
        _mode = Mode(mode)
        mode = _mode
        _mode.validate_bin()
        _path = self.validatepath(path)

        log.debug("openbin: %s, %s", path, mode)
        with self._lock:
            try:
                info = self.getinfo(_path)
            except errors.ResourceNotFound:
                if not _mode.create:
                    raise

                # Target doesn't exist and we're in create mode. Ensure the
                # parent is an existing directory before we try to create a file
                # in it.
                parent_path = self.get_parent(_path)

                # Can't use self.isdir() because it doesn't crash if the
                # directory doesn't exist, and we don't want to stat a file twice
                # if we can avoid it.
                info = self.getinfo(parent_path)
                if not info.is_dir:
                    raise errors.DirectoryExpected(parent_path)
                return DropboxFile(self.dropbox, path, mode)

            # Target exists.
            if info.is_dir:
                raise errors.FileExpected(path)
            if _mode.exclusive:
                raise errors.FileExists(path)
            return DropboxFile(self.dropbox, path, mode)

    def remove(self, path):
        _path = self.fix_path(path)

        try:
            info = self.getinfo(path)
            if info.is_dir:
                raise errors.FileExpected(path=path)
            self.dropbox.files_delete_v2(_path)
        except ApiError as e:
            if isinstance(e.error._value, LookupError):
                raise errors.ResourceNotFound(path=path)
            log.debug(e)
            raise errors.FileExpected(path=path, exc=e)

    def removedir(self, path):
        _path = self.fix_path(path)

        if _path == "/":
            raise errors.RemoveRootError()

        try:
            info = self.getinfo(path)
            if not info.is_dir:
                raise errors.DirectoryExpected(path=path)
            if len(self.listdir(path)) > 0:
                raise errors.DirectoryNotEmpty(path=path)
            self.dropbox.files_delete_v2(_path)
        except ApiError as e:
            if isinstance(e.error._value, LookupError):
                raise errors.ResourceNotFound(path=path)

            raise errors.FileExpected(path=path, exc=e)

    def copy(self, src_path, dst_path, overwrite=False):

        src_path = self.fix_path(src_path)
        dst_path = self.fix_path(dst_path)
        try:
            src_meta = self.getinfo(src_path)
            if src_meta.is_dir:
                raise errors.FileExpected(src_path)

        except ApiError as e:
            raise errors.ResourceNotFound
        dst_meta = None
        try:
            dst_meta = self.getinfo(dst_path)
        except Exception as e:
            pass

        if dst_meta is not None:
            if overwrite == True:
                self.remove(dst_path)
            else:
                raise errors.DestinationExists(dst_path)
        parent_path = self.get_parent(dst_path)

        if not self.exists(parent_path):
            raise errors.ResourceNotFound(dst_path)

        self.dropbox.files_copy_v2(src_path, dst_path)

    def get_parent(self, dst_path):
        import os

        parent_path = os.path.abspath(os.path.join(dst_path, ".."))
        return parent_path

    def exists(self, path):
        path = self.fix_path(path)
        try:
            self.getinfo(path)
            return True
        except errors.ResourceNotFound as e:
            return False

    def move(self, src_path, dst_path, overwrite=False):
        _src_path = self.fix_path(src_path)
        _dst_path = self.fix_path(dst_path)

        if not self.getinfo(_src_path).is_file:
            raise errors.FileExpected(src_path)
        if not overwrite and self.exists(_dst_path):
            raise errors.DestinationExists(dst_path)
        if "/" in dst_path and not self.exists(self.get_parent(_dst_path)):
            raise errors.ResourceNotFound(src_path)
        with self._lock:
            try:
                if overwrite:
                    try:
                        # remove file anyways
                        self.dropbox.files_delete_v2(_dst_path)
                    except Exception as e:
                        pass

                self.dropbox.files_move_v2(_src_path, _dst_path)
            except ApiError as e:

                raise errors.ResourceNotFound(src_path, exc=e)

    def apierror_map(self, error):
        log.debug(error)
