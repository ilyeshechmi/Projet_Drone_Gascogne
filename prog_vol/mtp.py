"""Accès uniforme à un volume monté ou à un périphérique MTP."""

from __future__ import annotations

import hashlib
import ctypes
import ctypes.util
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


WAYPOINT_RELATIVE_PATH = Path("Android/data/dji.go.v5/files/waypoint")
MAX_REMOTE_ENTRIES = 5000


class StorageError(RuntimeError):
    """Le stockage de la radiocommande est absent ou inaccessible."""


@dataclass(frozen=True)
class RemoteEntry:
    location: str
    name: str
    is_directory: bool
    size: int
    modified: int


def _sha256_stream(stream) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


class MountedStorage:
    """Stockage visible comme un dossier normal, utile aussi pour les tests."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        if not self.root.is_dir():
            raise StorageError(f"Racine de stockage introuvable : {self.root}")
        self.description = f"volume monté {self.root}"

    def _confined(self, location: str | Path) -> Path:
        candidate = Path(location).expanduser().resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise StorageError(f"Chemin extérieur au stockage autorisé : {candidate}") from exc
        return candidate

    def join(self, base: str, *parts: str) -> str:
        return str(self._confined(Path(base).joinpath(*parts)))

    def from_root(self, relative: str | Path) -> str:
        return str(self._confined(self.root / Path(relative)))

    def exists(self, location: str) -> bool:
        return self._confined(location).exists()

    def is_directory(self, location: str) -> bool:
        try:
            return self._confined(location).is_dir()
        except StorageError:
            return False

    def list_children(self, location: str) -> list[RemoteEntry]:
        try:
            children = list(self._confined(location).iterdir())
        except OSError as exc:
            raise StorageError(f"Impossible de lire {location} : {exc}") from exc
        result: list[RemoteEntry] = []
        for child in children:
            if child.name.startswith(".") or child.is_symlink():
                continue
            try:
                stat = child.stat()
            except OSError:
                continue
            result.append(
                RemoteEntry(
                    location=str(child),
                    name=child.name,
                    is_directory=child.is_dir(),
                    size=stat.st_size,
                    modified=int(stat.st_mtime),
                )
            )
        return result

    def copy_to_local(self, source: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._confined(source), destination)

    def sha256(self, location: str) -> str:
        with self._confined(location).open("rb") as stream:
            return _sha256_stream(stream)

    def size(self, location: str) -> int:
        return self._confined(location).stat().st_size

    def replace_from_local(self, source: Path, destination: str) -> None:
        target = self._confined(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".part",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
            temporary = None
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)


class GioMTPStorage:
    """Stockage MTP adressé avec des URI mtp:// par la commande gio."""

    def __init__(self, root_uri: str, gio: str) -> None:
        self.root = root_uri.rstrip("/")
        self.gio = gio
        self.description = f"périphérique MTP {self.root}"
        if not self.exists(self.root):
            raise StorageError(f"URI MTP inaccessible : {self.root}")

    def _run(self, arguments: list[str], *, binary: bool = False):
        environment = os.environ.copy()
        environment["LC_ALL"] = "C"
        try:
            result = subprocess.run(
                [self.gio, *arguments],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", b"")
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace").strip()
            raise StorageError(
                f"Échec de gio {' '.join(arguments)}" + (f" : {detail}" if detail else "")
            ) from exc
        return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")

    def join(self, base: str, *parts: str) -> str:
        encoded = "/".join(quote(part.strip("/"), safe="") for part in parts)
        return f"{base.rstrip('/')}/{encoded}" if encoded else base.rstrip("/")

    def from_root(self, relative: str | Path) -> str:
        return self.join(self.root, *Path(relative).parts)

    def _info(self, location: str) -> dict[str, int]:
        output = self._run(
            ["info", "-a", "standard::type,standard::size,time::modified", location]
        )
        values: dict[str, int] = {}
        for key in ("standard::type", "standard::size", "time::modified"):
            match = re.search(rf"^\s*{re.escape(key)}:\s*(\d+)", output, re.MULTILINE)
            if match:
                values[key] = int(match.group(1))
        return values

    def exists(self, location: str) -> bool:
        try:
            self._info(location)
            return True
        except StorageError:
            return False

    def is_directory(self, location: str) -> bool:
        try:
            return self._info(location).get("standard::type") == 2
        except StorageError:
            return False

    def list_children(self, location: str) -> list[RemoteEntry]:
        output = self._run(["list", "-u", location])
        result: list[RemoteEntry] = []
        for uri in (line.strip() for line in output.splitlines()):
            if not uri:
                continue
            name = unquote(uri.rstrip("/").rsplit("/", 1)[-1])
            info = self._info(uri)
            result.append(
                RemoteEntry(
                    location=uri,
                    name=name,
                    is_directory=info.get("standard::type") == 2,
                    size=info.get("standard::size", 0),
                    modified=info.get("time::modified", 0),
                )
            )
        return result

    def copy_to_local(self, source: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(["copy", "-T", source, str(destination)])

    def sha256(self, location: str) -> str:
        data = self._run(["cat", location], binary=True)
        return hashlib.sha256(data).hexdigest()

    def size(self, location: str) -> int:
        return self._info(location).get("standard::size", 0)

    def replace_from_local(self, source: Path, destination: str) -> None:
        unique = f"{os.getpid()}-{time.time_ns()}"
        temporary = destination + f".part-{unique}"
        remote_backup = destination + f".previous-{unique}"
        with source.open("rb") as source_stream:
            source_digest = _sha256_stream(source_stream)
        old_digest: str | None = None
        old_moved = False
        replacement_complete = False
        recovery_failed = False
        try:
            self._run(["copy", "-T", str(source), temporary])
            if self.sha256(temporary) != source_digest:
                raise StorageError("Le fichier MTP temporaire est corrompu.")
            if self.exists(destination):
                old_digest = self.sha256(destination)
                # Le drapeau est levé avant la commande : si Ctrl-C arrive juste
                # après le renommage distant, le bloc de récupération cherchera
                # quand même la copie .previous.
                old_moved = True
                self._run(["move", "-T", destination, remote_backup])
            self._run(["move", "-T", temporary, destination])
            if self.sha256(destination) != source_digest:
                raise StorageError("Le fichier MTP final est corrompu.")
            replacement_complete = True
            if old_moved and self.exists(remote_backup):
                try:
                    self._run(["remove", remote_backup])
                except StorageError:
                    # La nouvelle cible est déjà vérifiée. Garder une copie
                    # distante supplémentaire est plus sûr qu'annuler le transfert.
                    pass
        except BaseException as exc:
            if old_moved and self.exists(remote_backup):
                try:
                    if self.exists(destination):
                        self._run(["remove", destination])
                    self._run(["move", "-T", remote_backup, destination])
                    if old_digest and self.sha256(destination) != old_digest:
                        raise StorageError("La restauration MTP ne correspond pas à l'original.")
                except Exception as recovery_exc:
                    recovery_failed = True
                    raise StorageError(
                        "Le remplacement MTP et la restauration ont échoué. "
                        f"Copie de secours distante : {remote_backup}. Détail : {recovery_exc}"
                    ) from exc
            raise
        finally:
            if not recovery_failed and self.exists(temporary):
                try:
                    self._run(["remove", temporary])
                except StorageError:
                    pass
            if replacement_complete and self.exists(remote_backup):
                try:
                    self._run(["remove", remote_backup])
                except StorageError:
                    pass


class _MTPDeviceEntry(ctypes.Structure):
    _fields_ = [
        ("vendor", ctypes.c_char_p),
        ("vendor_id", ctypes.c_uint16),
        ("product", ctypes.c_char_p),
        ("product_id", ctypes.c_uint16),
        ("device_flags", ctypes.c_uint32),
    ]


class _MTPRawDevice(ctypes.Structure):
    _fields_ = [
        ("device_entry", _MTPDeviceEntry),
        ("bus_location", ctypes.c_uint32),
        ("devnum", ctypes.c_uint8),
    ]


class _MTPDeviceStorage(ctypes.Structure):
    pass


_MTPDeviceStoragePointer = ctypes.POINTER(_MTPDeviceStorage)
_MTPDeviceStorage._fields_ = [
    ("id", ctypes.c_uint32),
    ("storage_type", ctypes.c_uint16),
    ("filesystem_type", ctypes.c_uint16),
    ("access_capability", ctypes.c_uint16),
    ("max_capacity", ctypes.c_uint64),
    ("free_space_in_bytes", ctypes.c_uint64),
    ("free_space_in_objects", ctypes.c_uint64),
    ("description", ctypes.c_char_p),
    ("volume_identifier", ctypes.c_char_p),
    ("next", _MTPDeviceStoragePointer),
    ("previous", _MTPDeviceStoragePointer),
]


class _MTPDevice(ctypes.Structure):
    _fields_ = [
        ("object_bitsize", ctypes.c_uint8),
        ("params", ctypes.c_void_p),
        ("usbinfo", ctypes.c_void_p),
        ("storage", _MTPDeviceStoragePointer),
    ]


_MTPDevicePointer = ctypes.POINTER(_MTPDevice)


class _MTPFile(ctypes.Structure):
    pass


_MTPFilePointer = ctypes.POINTER(_MTPFile)
_MTPFile._fields_ = [
    ("item_id", ctypes.c_uint32),
    ("parent_id", ctypes.c_uint32),
    ("storage_id", ctypes.c_uint32),
    ("filename", ctypes.c_void_p),
    ("filesize", ctypes.c_uint64),
    ("modification_date", ctypes.c_long),
    ("filetype", ctypes.c_int),
    ("next", _MTPFilePointer),
]


class _MTPFolder(ctypes.Structure):
    pass


_MTPFolderPointer = ctypes.POINTER(_MTPFolder)
_MTPFolder._fields_ = [
    ("folder_id", ctypes.c_uint32),
    ("parent_id", ctypes.c_uint32),
    ("storage_id", ctypes.c_uint32),
    ("name", ctypes.c_char_p),
    ("sibling", _MTPFolderPointer),
    ("child", _MTPFolderPointer),
]


class _MTPError(ctypes.Structure):
    pass


_MTPErrorPointer = ctypes.POINTER(_MTPError)
_MTPError._fields_ = [
    ("error_number", ctypes.c_int),
    ("error_text", ctypes.c_char_p),
    ("next", _MTPErrorPointer),
]


class _LibMTPBinding:
    """Déclarations ctypes limitées aux opérations utilisées par ce module."""

    def __init__(self, library_path: str) -> None:
        self.library = ctypes.CDLL(library_path)
        lib = self.library
        lib.LIBMTP_Init.argtypes = []
        lib.LIBMTP_Init.restype = None
        lib.LIBMTP_Detect_Raw_Devices.argtypes = [
            ctypes.POINTER(ctypes.POINTER(_MTPRawDevice)),
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.LIBMTP_Detect_Raw_Devices.restype = ctypes.c_int
        lib.LIBMTP_Open_Raw_Device.argtypes = [ctypes.POINTER(_MTPRawDevice)]
        lib.LIBMTP_Open_Raw_Device.restype = _MTPDevicePointer
        lib.LIBMTP_Release_Device.argtypes = [_MTPDevicePointer]
        lib.LIBMTP_Release_Device.restype = None
        lib.LIBMTP_Get_Storage.argtypes = [_MTPDevicePointer, ctypes.c_int]
        lib.LIBMTP_Get_Storage.restype = ctypes.c_int
        lib.LIBMTP_Get_Files_And_Folders.argtypes = [
            _MTPDevicePointer,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        lib.LIBMTP_Get_Files_And_Folders.restype = _MTPFilePointer
        lib.LIBMTP_Get_Filelisting_With_Callback.argtypes = [
            _MTPDevicePointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        lib.LIBMTP_Get_Filelisting_With_Callback.restype = _MTPFilePointer
        lib.LIBMTP_Get_Folder_List_For_Storage.argtypes = [
            _MTPDevicePointer,
            ctypes.c_uint32,
        ]
        lib.LIBMTP_Get_Folder_List_For_Storage.restype = _MTPFolderPointer
        lib.LIBMTP_destroy_folder_t.argtypes = [_MTPFolderPointer]
        lib.LIBMTP_destroy_folder_t.restype = None
        lib.LIBMTP_Get_Filemetadata.argtypes = [_MTPDevicePointer, ctypes.c_uint32]
        lib.LIBMTP_Get_Filemetadata.restype = _MTPFilePointer
        lib.LIBMTP_Get_File_To_File.argtypes = [
            _MTPDevicePointer,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        lib.LIBMTP_Get_File_To_File.restype = ctypes.c_int
        lib.LIBMTP_Send_File_From_File.argtypes = [
            _MTPDevicePointer,
            ctypes.c_char_p,
            _MTPFilePointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        lib.LIBMTP_Send_File_From_File.restype = ctypes.c_int
        lib.LIBMTP_Set_File_Name.argtypes = [
            _MTPDevicePointer,
            _MTPFilePointer,
            ctypes.c_char_p,
        ]
        lib.LIBMTP_Set_File_Name.restype = ctypes.c_int
        lib.LIBMTP_Delete_Object.argtypes = [_MTPDevicePointer, ctypes.c_uint32]
        lib.LIBMTP_Delete_Object.restype = ctypes.c_int
        lib.LIBMTP_new_file_t.argtypes = []
        lib.LIBMTP_new_file_t.restype = _MTPFilePointer
        lib.LIBMTP_destroy_file_t.argtypes = [_MTPFilePointer]
        lib.LIBMTP_destroy_file_t.restype = None
        lib.LIBMTP_Get_Errorstack.argtypes = [_MTPDevicePointer]
        lib.LIBMTP_Get_Errorstack.restype = _MTPErrorPointer
        lib.LIBMTP_Clear_Errorstack.argtypes = [_MTPDevicePointer]
        lib.LIBMTP_Clear_Errorstack.restype = None
        lib.LIBMTP_FreeMemory.argtypes = [ctypes.c_void_p]
        lib.LIBMTP_FreeMemory.restype = None
        lib.LIBMTP_Init()


def _libmtp_library_path() -> str | None:
    discovered = ctypes.util.find_library("mtp")
    candidates = [
        discovered,
        "/opt/homebrew/lib/libmtp.dylib",
        "/usr/local/lib/libmtp.dylib",
        "/usr/lib/libmtp.so",
    ]
    return next((path for path in candidates if path and Path(path).exists()), discovered)


@lru_cache(maxsize=1)
def _libmtp_binding() -> _LibMTPBinding | None:
    path = _libmtp_library_path()
    if not path:
        return None
    try:
        return _LibMTPBinding(path)
    except (AttributeError, OSError):
        return None


def _decode(value: bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if value else ""


@dataclass(frozen=True)
class _MTPObject:
    item_id: int
    parent_id: int
    storage_id: int
    name: str
    is_directory: bool
    size: int
    modified: int
    location: str

    def remote_entry(self) -> RemoteEntry:
        return RemoteEntry(
            location=self.location,
            name=self.name,
            is_directory=self.is_directory,
            size=self.size,
            modified=self.modified,
        )


class _LibMTPSession:
    def __init__(self, binding: _LibMTPBinding, device: _MTPDevicePointer) -> None:
        self.binding = binding
        self.device = device
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.binding.library.LIBMTP_Release_Device(self.device)
            self.closed = True


class LibMTPStorage:
    """Stockage MTP natif utilisant libmtp sans montage ni OpenMTP."""

    _ROOT_OBJECT_ID = 0

    def __init__(
        self,
        session: _LibMTPSession,
        storage_id: int,
        device_name: str,
        storage_name: str,
    ) -> None:
        self._session = session
        self._binding = session.binding
        self._device = session.device
        self.storage_id = storage_id
        self._inventory: dict[int, list[_MTPObject]] | None = None
        self.root = f"libmtp://{storage_id:08x}"
        label = " / ".join(part for part in (device_name, storage_name) if part)
        self.description = f"périphérique MTP libmtp {label or self.root}"

    def close(self) -> None:
        self._session.close()

    def _ensure_open(self) -> None:
        if self._session.closed:
            raise StorageError("La session libmtp est déjà fermée.")

    def _error_details(self) -> str:
        errors = self._binding.library.LIBMTP_Get_Errorstack(self._device)
        details: list[str] = []
        current = errors
        while current:
            text = current.contents.error_text
            if text:
                details.append(_decode(text))
            current = current.contents.next
        self._binding.library.LIBMTP_Clear_Errorstack(self._device)
        return " | ".join(details)

    def _start_operation(self) -> None:
        self._ensure_open()
        self._binding.library.LIBMTP_Clear_Errorstack(self._device)

    def _failed(self, operation: str) -> StorageError:
        detail = self._error_details()
        return StorageError(f"Échec libmtp ({operation})" + (f" : {detail}" if detail else ""))

    def _parts(self, location: str) -> tuple[str, ...]:
        parsed = urlsplit(location)
        if parsed.scheme != "libmtp" or parsed.netloc.casefold() != f"{self.storage_id:08x}":
            raise StorageError(f"Chemin extérieur au stockage libmtp : {location}")
        parts = tuple(unquote(part) for part in parsed.path.split("/") if part)
        if any(part in {".", ".."} or "/" in part for part in parts):
            raise StorageError(f"Chemin libmtp invalide : {location}")
        return parts

    def _uri(self, parts: tuple[str, ...]) -> str:
        suffix = "/".join(quote(part, safe="") for part in parts)
        return f"{self.root}/{suffix}" if suffix else self.root

    def join(self, base: str, *parts: str) -> str:
        combined = [*self._parts(base)]
        for part in parts:
            cleaned = part.strip("/")
            if not cleaned or cleaned in {".", ".."} or "/" in cleaned:
                raise StorageError(f"Composant de chemin libmtp invalide : {part}")
            combined.append(cleaned)
        return self._uri(tuple(combined))

    def from_root(self, relative: str | Path) -> str:
        parts = Path(relative).parts
        if Path(relative).is_absolute() or any(part in {".", ".."} for part in parts):
            raise StorageError(f"Chemin relatif libmtp invalide : {relative}")
        return self.join(self.root, *parts)

    def _load_inventory(self) -> dict[int, list[_MTPObject]]:
        self._start_operation()
        inventory: dict[int, list[_MTPObject]] = {}
        folders = self._binding.library.LIBMTP_Get_Folder_List_For_Storage(
            self._device,
            self.storage_id,
        )

        def add_folder_chain(current: _MTPFolderPointer) -> None:
            while current:
                metadata = current.contents
                name = _decode(metadata.name)
                inventory.setdefault(int(metadata.parent_id), []).append(
                    _MTPObject(
                        item_id=int(metadata.folder_id),
                        parent_id=int(metadata.parent_id),
                        storage_id=int(metadata.storage_id),
                        name=name,
                        is_directory=True,
                        size=0,
                        modified=0,
                        location="",
                    )
                )
                if metadata.child:
                    add_folder_chain(metadata.child)
                current = metadata.sibling

        if folders:
            try:
                add_folder_chain(folders)
            finally:
                self._binding.library.LIBMTP_destroy_folder_t(folders)

        current = self._binding.library.LIBMTP_Get_Filelisting_With_Callback(
            self._device,
            None,
            None,
        )
        while current:
            metadata = current.contents
            # Une instance ctypes pointant dans la structure libérée deviendrait
            # invalide : conserver l'adresse brute du prochain maillon.
            next_address = ctypes.cast(metadata.next, ctypes.c_void_p).value
            name = ctypes.string_at(metadata.filename).decode("utf-8", errors="replace")
            inventory.setdefault(int(metadata.parent_id), []).append(
                _MTPObject(
                    item_id=int(metadata.item_id),
                    parent_id=int(metadata.parent_id),
                    storage_id=int(metadata.storage_id),
                    name=name,
                    is_directory=metadata.filetype == 0,
                    size=int(metadata.filesize),
                    modified=int(metadata.modification_date),
                    location="",
                )
            )
            self._binding.library.LIBMTP_destroy_file_t(current)
            current = (
                ctypes.cast(next_address, _MTPFilePointer)
                if next_address is not None
                else _MTPFilePointer()
            )
        detail = self._error_details()
        if detail:
            raise StorageError(f"Échec libmtp (inventaire du stockage) : {detail}")
        self._inventory = inventory
        return inventory

    def _children(self, parent_id: int, parent_location: str) -> list[_MTPObject]:
        inventory = self._inventory if self._inventory is not None else self._load_inventory()
        return [
            _MTPObject(
                item_id=item.item_id,
                parent_id=item.parent_id,
                storage_id=item.storage_id,
                name=item.name,
                is_directory=item.is_directory,
                size=item.size,
                modified=item.modified,
                location=self.join(parent_location, item.name),
            )
            for item in inventory.get(parent_id, [])
        ]

    def _resolve(self, location: str) -> _MTPObject:
        parts = self._parts(location)
        if not parts:
            return _MTPObject(
                item_id=self._ROOT_OBJECT_ID,
                parent_id=0,
                storage_id=self.storage_id,
                name="",
                is_directory=True,
                size=0,
                modified=0,
                location=self.root,
            )
        parent_id = self._ROOT_OBJECT_ID
        parent_location = self.root
        selected: _MTPObject | None = None
        for name in parts:
            matching = [item for item in self._children(parent_id, parent_location) if item.name == name]
            if len(matching) != 1:
                if not matching:
                    raise StorageError(f"Objet MTP introuvable : {location}")
                raise StorageError(f"Nom MTP ambigu dans {parent_location} : {name}")
            selected = matching[0]
            parent_id = selected.item_id
            parent_location = selected.location
        assert selected is not None
        return selected

    def exists(self, location: str) -> bool:
        try:
            self._resolve(location)
            return True
        except StorageError:
            return False

    def is_directory(self, location: str) -> bool:
        try:
            return self._resolve(location).is_directory
        except StorageError:
            return False

    def list_children(self, location: str) -> list[RemoteEntry]:
        parent = self._resolve(location)
        if not parent.is_directory:
            raise StorageError(f"L'objet MTP n'est pas un dossier : {location}")
        return [item.remote_entry() for item in self._children(parent.item_id, location)]

    def _download(self, item_id: int, destination: Path) -> None:
        if destination.exists():
            raise StorageError(f"Le fichier local existe déjà : {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._start_operation()
        result = self._binding.library.LIBMTP_Get_File_To_File(
            self._device,
            item_id,
            os.fsencode(destination),
            None,
            None,
        )
        if result != 0:
            destination.unlink(missing_ok=True)
            raise self._failed(f"téléchargement vers {destination}")

    def copy_to_local(self, source: str, destination: Path) -> None:
        item = self._resolve(source)
        if item.is_directory:
            raise StorageError(f"Impossible de télécharger un dossier MTP : {source}")
        self._download(item.item_id, destination)

    def _sha256_id(self, item_id: int) -> str:
        with tempfile.TemporaryDirectory(prefix="prog-vol-libmtp-") as directory:
            path = Path(directory) / "object"
            self._download(item_id, path)
            with path.open("rb") as stream:
                return _sha256_stream(stream)

    def sha256(self, location: str) -> str:
        return self._sha256_id(self._resolve(location).item_id)

    def size(self, location: str) -> int:
        return self._resolve(location).size

    def _send(self, source: Path, parent: _MTPObject, filename: str) -> int:
        metadata = self._binding.library.LIBMTP_new_file_t()
        if not metadata:
            raise StorageError("Impossible d'allouer les métadonnées libmtp.")
        encoded_name = filename.encode("utf-8")
        name_buffer = ctypes.create_string_buffer(encoded_name)
        try:
            metadata.contents.parent_id = parent.item_id
            metadata.contents.storage_id = self.storage_id
            metadata.contents.filename = ctypes.cast(name_buffer, ctypes.c_void_p)
            metadata.contents.filesize = source.stat().st_size
            self._start_operation()
            result = self._binding.library.LIBMTP_Send_File_From_File(
                self._device,
                os.fsencode(source),
                metadata,
                None,
                None,
            )
            if result != 0:
                raise self._failed(f"envoi de {filename}")
            self._inventory = None
            return int(metadata.contents.item_id)
        finally:
            # Le tampon Python ne doit pas être libéré par LIBMTP_destroy_file_t.
            metadata.contents.filename = None
            self._binding.library.LIBMTP_destroy_file_t(metadata)

    def _rename(self, item_id: int, new_name: str) -> None:
        self._start_operation()
        metadata = self._binding.library.LIBMTP_Get_Filemetadata(self._device, item_id)
        if not metadata:
            raise self._failed(f"lecture des métadonnées de l'objet {item_id}")
        try:
            result = self._binding.library.LIBMTP_Set_File_Name(
                self._device,
                metadata,
                new_name.encode("utf-8"),
            )
            if result != 0:
                raise self._failed(f"renommage de l'objet {item_id}")
            self._inventory = None
        finally:
            self._binding.library.LIBMTP_destroy_file_t(metadata)

    def _delete(self, item_id: int) -> None:
        self._start_operation()
        if self._binding.library.LIBMTP_Delete_Object(self._device, item_id) != 0:
            raise self._failed(f"suppression de l'objet {item_id}")
        self._inventory = None

    def replace_from_local(self, source: Path, destination: str) -> None:
        destination_parts = self._parts(destination)
        if not destination_parts:
            raise StorageError("La racine libmtp ne peut pas être remplacée.")
        destination_name = destination_parts[-1]
        parent_location = self._uri(destination_parts[:-1])
        parent = self._resolve(parent_location)
        old = self._resolve(destination) if self.exists(destination) else None
        unique = f"{os.getpid()}-{time.time_ns()}"
        temporary_name = f".{destination_name}.part-{unique}"
        backup_name = f".{destination_name}.previous-{unique}"
        with source.open("rb") as stream:
            source_digest = _sha256_stream(stream)

        uploaded_id: int | None = None
        old_digest: str | None = None
        old_moved = False
        new_named = False
        replacement_complete = False
        recovery_failed = False
        try:
            uploaded_id = self._send(source, parent, temporary_name)
            if self._sha256_id(uploaded_id) != source_digest:
                raise StorageError("Le fichier libmtp temporaire est corrompu.")
            if old:
                old_digest = self._sha256_id(old.item_id)
                old_moved = True
                self._rename(old.item_id, backup_name)
            new_named = True
            self._rename(uploaded_id, destination_name)
            if self._sha256_id(uploaded_id) != source_digest:
                raise StorageError("Le fichier libmtp final est corrompu.")
            replacement_complete = True
            if old:
                try:
                    self._delete(old.item_id)
                    old = None
                except StorageError:
                    pass
        except BaseException as exc:
            if old_moved and old:
                try:
                    if new_named and uploaded_id is not None:
                        self._delete(uploaded_id)
                        uploaded_id = None
                    self._rename(old.item_id, destination_name)
                    if old_digest and self._sha256_id(old.item_id) != old_digest:
                        raise StorageError("La restauration libmtp ne correspond pas à l'original.")
                except Exception as recovery_exc:
                    recovery_failed = True
                    raise StorageError(
                        "Le remplacement libmtp et la restauration ont échoué. "
                        f"Objet de secours distant : {old.item_id}. Détail : {recovery_exc}"
                    ) from exc
            raise
        finally:
            if not recovery_failed and not replacement_complete and uploaded_id is not None:
                try:
                    self._delete(uploaded_id)
                except StorageError:
                    pass


def _gio_executable() -> str | None:
    return shutil.which("gio")


def _mounted_candidates() -> list[MountedStorage]:
    volumes = Path("/Volumes")
    if not volumes.is_dir():
        return []
    result: list[MountedStorage] = []
    for child in volumes.iterdir():
        if child.is_dir():
            try:
                result.append(MountedStorage(child))
            except StorageError:
                pass
    return result


def _gio_mtp_candidates(gio: str) -> list[GioMTPStorage]:
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    try:
        output = subprocess.run(
            [gio, "mount", "-li"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=30,
        ).stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return []
    uris = sorted(set(re.findall(r"mtp://[^\s]+", output)))
    result: list[GioMTPStorage] = []
    for uri in uris:
        try:
            result.append(GioMTPStorage(uri, gio))
        except StorageError:
            pass
    return result


def _libmtp_candidates() -> list[LibMTPStorage]:
    binding = _libmtp_binding()
    if not binding:
        return []
    raw_devices = ctypes.POINTER(_MTPRawDevice)()
    device_count = ctypes.c_int()
    result = binding.library.LIBMTP_Detect_Raw_Devices(
        ctypes.byref(raw_devices),
        ctypes.byref(device_count),
    )
    if result != 0 or not raw_devices or device_count.value <= 0:
        if raw_devices:
            binding.library.LIBMTP_FreeMemory(raw_devices)
        return []

    candidates: list[LibMTPStorage] = []
    try:
        for index in range(device_count.value):
            raw = raw_devices[index]
            vendor = _decode(raw.device_entry.vendor)
            product = _decode(raw.device_entry.product)
            device_name = " ".join(part for part in (vendor, product) if part)
            device = binding.library.LIBMTP_Open_Raw_Device(
                ctypes.byref(raw_devices[index])
            )
            if not device:
                continue
            session = _LibMTPSession(binding, device)
            binding.library.LIBMTP_Clear_Errorstack(device)
            if binding.library.LIBMTP_Get_Storage(device, 0) != 0:
                session.close()
                continue
            storage = device.contents.storage
            before = len(candidates)
            while storage:
                info = storage.contents
                candidates.append(
                    LibMTPStorage(
                        session,
                        int(info.id),
                        device_name,
                        _decode(info.description),
                    )
                )
                storage = info.next
            if len(candidates) == before:
                session.close()
    finally:
        binding.library.LIBMTP_FreeMemory(raw_devices)
    return candidates


def _close_native_candidates(
    candidates: list[LibMTPStorage], keep: LibMTPStorage | None = None
) -> None:
    kept_session = keep._session if keep else None
    closed_sessions: set[int] = set()
    for candidate in candidates:
        session = candidate._session
        identity = id(session)
        if session is kept_session or identity in closed_sessions:
            continue
        session.close()
        closed_sessions.add(identity)


def detect_storage(
    *, device_root: str | None = None, mtp_uri: str | None = None
) -> MountedStorage | GioMTPStorage | LibMTPStorage:
    """Détecte le support, avec options explicites utiles au diagnostic."""
    if device_root and mtp_uri:
        raise StorageError("Utilisez soit --device-root, soit --mtp-uri, pas les deux.")
    if device_root:
        return MountedStorage(Path(device_root))

    gio = _gio_executable()
    if mtp_uri:
        if not gio:
            raise StorageError("La commande gio est nécessaire pour utiliser --mtp-uri.")
        return GioMTPStorage(mtp_uri, gio)

    candidates: list[MountedStorage | GioMTPStorage] = _mounted_candidates()
    if gio:
        candidates.extend(_gio_mtp_candidates(gio))
    compatible: list[MountedStorage | GioMTPStorage] = []
    for storage in candidates:
        try:
            locate_waypoint_directory(storage)
            compatible.append(storage)
        except StorageError:
            continue
    if len(compatible) == 1:
        return compatible[0]
    if len(compatible) > 1:
        descriptions = ", ".join(storage.description for storage in compatible)
        raise StorageError(
            "Plusieurs stockages DJI Fly sont accessibles. Précisez --device-root "
            f"ou --mtp-uri. Stockages : {descriptions}"
        )

    native_candidates = _libmtp_candidates()
    native_compatible: list[LibMTPStorage] = []
    for storage in native_candidates:
        try:
            locate_waypoint_directory(storage)
            native_compatible.append(storage)
        except StorageError:
            continue
    if len(native_compatible) == 1:
        selected = native_compatible[0]
        _close_native_candidates(native_candidates, keep=selected)
        return selected
    _close_native_candidates(native_candidates)
    if len(native_compatible) > 1:
        descriptions = ", ".join(storage.description for storage in native_compatible)
        raise StorageError(
            "Plusieurs stockages DJI Fly sont accessibles avec libmtp. "
            f"Stockages : {descriptions}"
        )
    raise StorageError(
        "Aucun stockage DJI Fly accessible. Branchez la radiocommande en mode "
        "transfert de fichiers, installez libmtp, ou indiquez --device-root / --mtp-uri."
    )


def locate_waypoint_directory(
    storage: MountedStorage | GioMTPStorage | LibMTPStorage,
    override: str | None = None,
) -> str:
    if override:
        location = override if override.startswith("mtp://") else storage.from_root(override)
        if not storage.is_directory(location):
            raise StorageError(f"Dossier waypoint inaccessible : {location}")
        return location

    direct = storage.from_root(WAYPOINT_RELATIVE_PATH)
    if storage.is_directory(direct):
        return direct

    # Certaines radiocommandes ajoutent un niveau "Internal shared storage".
    for child in storage.list_children(str(storage.root)):
        if not child.is_directory:
            continue
        candidate = storage.join(child.location, *WAYPOINT_RELATIVE_PATH.parts)
        if storage.is_directory(candidate):
            return candidate
    raise StorageError(
        f"Dossier Android/data/dji.go.v5/files/waypoint introuvable sur {storage.description}."
    )


def list_mission_files(
    storage: MountedStorage | GioMTPStorage | LibMTPStorage, waypoint_directory: str
) -> list[RemoteEntry]:
    missions: list[RemoteEntry] = []
    pending = [waypoint_directory]
    visited = 0
    while pending:
        current = pending.pop()
        for entry in storage.list_children(current):
            visited += 1
            if visited > MAX_REMOTE_ENTRIES:
                raise StorageError("Le dossier waypoint contient trop d'éléments.")
            if entry.is_directory:
                pending.append(entry.location)
            elif entry.name.casefold().endswith(".kmz") and not entry.name.startswith("."):
                missions.append(entry)
    return missions
