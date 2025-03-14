import enum
import errno
import hashlib
import json
import os
import posixpath
import sys
from collections import deque
from dataclasses import asdict
from itertools import accumulate
from pathlib import Path
from posixpath import relpath
from typing import List, Optional

import click
import typer  # pylint: disable=import-error
from dvc_objects._tqdm import Tqdm
from dvc_objects.errors import ObjectFormatError
from dvc_objects.fs import LocalFileSystem, MemoryFileSystem
from dvc_objects.fs.callbacks import Callback
from dvc_objects.fs.utils import human_readable_to_bytes
from rich.traceback import install

from dvc_data import load
from dvc_data.build import build as _build
from dvc_data.checkout import checkout as _checkout
from dvc_data.diff import ROOT
from dvc_data.diff import diff as _diff
from dvc_data.hashfile.db import HashFileDB
from dvc_data.hashfile.hash import file_md5 as _file_md5
from dvc_data.hashfile.hash import fobj_md5 as _fobj_md5
from dvc_data.hashfile.hash_info import HashInfo
from dvc_data.hashfile.obj import HashFile
from dvc_data.hashfile.state import State
from dvc_data.objects.tree import Tree
from dvc_data.objects.tree import du as _du
from dvc_data.objects.tree import merge
from dvc_data.repo import NotARepo, Repo
from dvc_data.transfer import transfer as _transfer

install(show_locals=True, suppress=[typer, click])


file_type = typer.Argument(
    ...,
    exists=True,
    file_okay=True,
    dir_okay=False,
    readable=True,
    resolve_path=True,
    allow_dash=True,
    path_type=str,
)
dir_file_type = typer.Argument(
    ...,
    exists=True,
    file_okay=True,
    dir_okay=True,
    readable=True,
    resolve_path=True,
    allow_dash=True,
    path_type=str,
)

HashEnum = enum.Enum(  # type: ignore[misc]
    "HashEnum", {h: h for h in sorted(hashlib.algorithms_available)}
)
LinkEnum = enum.Enum(  # type: ignore[misc]
    "LinkEnum", {lt: lt for lt in ["reflink", "hardlink", "symlink", "copy"]}
)
SIZE_HELP = "Human readable size, eg: '1kb', '100Mb', '10GB' etc"


class Application(typer.Typer):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("no_args_is_help", True)
        super().__init__(*args, **kwargs)

    def command(self, *args, **kwargs):
        kwargs.setdefault("no_args_is_help", True)
        return super().command(*args, **kwargs)


app = Application(
    name="dvc-data",
    help="dvc-data testingtool",
    add_completion=False,
)


@app.command(name="hash", help="Compute checksum of the file")
def hash_file(
    file: Path = file_type,
    name: HashEnum = typer.Option("md5", "-n", "--name"),
    progress: bool = typer.Option(False, "--progress", "-p"),
    text: Optional[bool] = typer.Option(None, "--text/--binary", "-t/-b"),
):
    path = relpath(file)
    hash_name = name.value
    callback = Callback.as_callback()
    if progress:
        callback = Callback.as_tqdm_callback(
            desc=f"hashing {path} with {hash_name}", bytes=True
        )

    with callback:
        if path == "-":
            fobj = callback.wrap_attr(sys.stdin.buffer)
            hash_value = _fobj_md5(fobj, text=text, name=hash_name)
        else:
            hash_value = _file_md5(
                path, name=hash_name, callback=callback, text=text
            )
    typer.echo(f"{hash_name}: {hash_value}")


@app.command(help="Generate sparse file")
def gensparse(
    file: Path = typer.Argument(..., allow_dash=True),
    size: str = typer.Argument(..., help=SIZE_HELP),
):
    with file.open("wb") as f:
        f.seek(human_readable_to_bytes(size) - 1)
        f.write(b"\0")


@app.command(help="Generate file with random contents")
def genrand(
    file: Path = typer.Argument(..., allow_dash=True),
    size: str = typer.Argument(..., help=SIZE_HELP),
):
    with file.open("wb") as f:
        f.write(os.urandom(human_readable_to_bytes(size)))


def from_shortoid(odb: HashFileDB, oid: str) -> str:
    oid = oid if oid != "-" else sys.stdin.read().strip()
    try:
        return odb.exists_prefix(oid)
    except KeyError as exc:
        typer.echo(f"Not a valid {oid=}", err=True)
        raise typer.Exit(1) from exc
    except ValueError as exc:
        typer.echo(f"Ambiguous {oid=}", err=True)
        raise typer.Exit(1) from exc


def get_odb(**config):
    try:
        repo = Repo.discover()
    except NotARepo as exc:
        typer.echo(exc, err=True)
        raise typer.Abort(1)

    state = State(root_dir=repo.root, tmp_dir=repo.tmp_dir)
    return HashFileDB(repo.fs, repo.object_dir, state=state, **config)


@app.command(help="Oid to path")
def o2p(oid: str = typer.Argument(..., allow_dash=True)):
    odb = get_odb()
    path = odb.oid_to_path(from_shortoid(odb, oid))
    typer.echo(path)


@app.command(help="Path to Oid")
def p2o(path: Path = typer.Argument(..., allow_dash=True)):
    odb = get_odb()
    fs_path = relpath(path)
    if fs_path == "-":
        fs_path = sys.stdin.read().strip()

    oid = odb.path_to_oid(fs_path)
    typer.echo(oid)


def _cat_object(odb, oid):
    path = odb.oid_to_path(oid)
    contents = odb.fs.cat_file(path)
    return typer.echo(contents)


@app.command(help="Provide content of the objects")
def cat(
    oid: str = typer.Argument(..., allow_dash=True),
    check: bool = typer.Option(False, "--check", "-c"),
):
    odb = get_odb()
    oid = from_shortoid(odb, oid)
    if check:
        try:
            return odb.check(oid, check_hash=True)
        except ObjectFormatError as exc:
            typer.echo(exc, err=True)
            raise typer.Exit(1) from exc
    return _cat_object(odb, oid)


@app.command(help="Build and optionally write object to the database")
def build(
    path: Path = dir_file_type,
    write: bool = typer.Option(False, "--write", "-w"),
    shallow: bool = False,
):
    odb = get_odb()
    fs_path = relpath(path)

    fs = odb.fs
    if fs_path == "-":
        fs = MemoryFileSystem()
        fs.put_file(sys.stdin.buffer, fs_path)

    object_store, _, obj = _build(odb, fs_path, fs, name="md5")
    if write:
        _transfer(
            object_store,
            odb,
            {obj.hash_info},
            hardlink=True,
            shallow=shallow,
        )
    typer.echo(obj)


def _ls_tree(tree):
    for key, (_, hash_info) in tree.iteritems():
        typer.echo(f"{hash_info.value}\t{posixpath.join(*key)}")


@app.command("ls", help="List objects in a tree")
@app.command("ls-tree", help="List objects in a tree")
def ls(oid: str = typer.Argument(..., allow_dash=True)):
    odb = get_odb()
    oid = from_shortoid(odb, oid)
    try:
        tree = Tree.load(odb, HashInfo("md5", oid))
    except ObjectFormatError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    return _ls_tree(tree)


@app.command(help="Show various types of objects")
def show(oid: str = typer.Argument(..., allow_dash=True)):
    odb = get_odb()
    oid = from_shortoid(odb, oid)
    obj = load(odb, odb.get(oid).hash_info)
    if isinstance(obj, Tree):
        return _ls_tree(obj)
    elif isinstance(obj, HashFile):
        return _cat_object(odb, obj.oid)
    raise AssertionError(f"unknown object of type {type(obj)}")


@app.command(help="Summarize disk usage by an object")
def du(oid: str = typer.Argument(..., allow_dash=True)):
    odb = get_odb()
    oid = from_shortoid(odb, oid)
    obj = load(odb, odb.get(oid).hash_info)
    if isinstance(obj, HashFile):
        tree = Tree()
        tree.add(ROOT, None, obj.hash_info)
    elif isinstance(obj, Tree):
        tree = obj
    else:
        raise AssertionError(f"unknown object of type {type(obj)}")
    total = _du(odb, tree)
    typer.echo(Tqdm.format_sizeof(total, suffix="B", divisor=1024))


@app.command(help="Remove object from the ODB")
def rm(oid: str = typer.Argument(..., allow_dash=True)):
    odb = get_odb()
    oid = from_shortoid(odb, oid)
    path = odb.oid_to_path(oid)
    odb.fs.remove(path)


@app.command(
    help="Count objects and their disk consumption", no_args_is_help=False
)
def count_objects():
    odb = get_odb()
    it = (odb.fs.size(odb.oid_to_path(oid)) for oid in odb.all())
    item = deque(enumerate(accumulate(it), 1), maxlen=1)
    count, total = item[0] if item else (0, 0)
    hsize = Tqdm.format_sizeof(total, suffix="B", divisor=1024)
    typer.echo(f"{count} objects, {hsize} size")


@app.command(help="Verify objects in the database", no_args_is_help=False)
def fsck():
    odb = get_odb()
    ret = 0
    for oid in odb.all():
        try:
            odb.check(oid, check_hash=True)
        except ObjectFormatError as exc:
            ret = 1
            typer.echo(exc)
    raise typer.Exit(ret)


@app.command(help="Diff two objects in the database")
def diff(short_oid1, short_oid2: str, unchanged: bool = False):
    odb = get_odb()
    obj1 = odb.get(from_shortoid(odb, short_oid1))
    obj2 = odb.get(from_shortoid(odb, short_oid2))
    d = _diff(load(odb, obj1.hash_info), load(odb, obj2.hash_info), odb)

    def _prepare_info(entry):
        path = posixpath.join(*entry.key) or "ROOT"
        oid = entry.oid.value
        if not oid.endswith(".dir"):
            oid = entry.oid.value[:9]
        cache_info = "" if entry.in_cache else ", missing"
        return f"{path} ({oid}{cache_info})"

    for state, changes in asdict(d).items():
        for change in changes:
            if not unchanged and state == "unchanged" and change.new.in_cache:
                continue
            if state == "modified":
                info1 = _prepare_info(change.old)
                info2 = _prepare_info(change.new)
                info = f"{info1} -> {info2}"
            elif state == "added":
                info = _prepare_info(change.new)
            else:
                # for unchanged, it does not matter which entry we use
                # for deleted, we should be using old entry
                info = _prepare_info(change.old)
            typer.echo(f"{state}: {info}")


@app.command(help="Merge two trees and optionally write to the database.")
def merge_tree(oid1: str, oid2: str, force: bool = False):
    odb = get_odb()
    oid1 = from_shortoid(odb, oid1)
    oid2 = from_shortoid(odb, oid2)
    obj1 = load(odb, odb.get(oid1).hash_info)
    obj2 = load(odb, odb.get(oid2).hash_info)
    assert isinstance(obj1, Tree) and isinstance(obj2, Tree), "not a tree obj"

    if not force:
        # detect conflicts
        d = _diff(obj1, obj2, odb)
        modified = [
            posixpath.join(*change.old.key)
            for change in d.modified
            if change.old.key != ROOT
        ]
        if modified:
            typer.echo("Following files in conflicts:")
            for file in modified:
                typer.echo(file)
            raise typer.Exit(1)

    tree = merge(odb, None, obj1.hash_info, obj2.hash_info)
    tree.digest()
    typer.echo(tree)
    odb.add(tree.path, tree.fs, tree.oid, hardlink=True)


def process_patch(patch_file, **kwargs):
    patch = []
    if patch_file:
        with typer.open_file(patch_file) as f:
            text = f.read()
            patch = json.loads(text)
            for appl in patch:
                op = appl.get("op")
                path = appl.get("path")
                if op and path and op in ("add", "modify"):
                    patch[appl]["path"] = os.fspath(
                        patch_file.parent.joinpath(path)
                    )

    for op, items in kwargs.items():
        for item in items:
            if isinstance(item, tuple):
                path, to = item
                extra = {"path": path, "to": to}
            else:
                extra = {"path": item}
            patch.append({"op": op, **extra})

    return patch


def apply_op(odb, obj, application):
    assert "op" in application
    op = application["op"]
    path = application["path"]
    keys = tuple(path.split("/"))
    # pylint: disable=protected-access
    if op in ("add", "modify"):
        new = tuple(application["to"].split("/"))
        if op == "add" and new in obj._dict:
            raise FileExistsError(
                errno.EEXIST, os.strerror(errno.EEXIST), path
            )

        fs = LocalFileSystem()
        _, meta, new_obj = _build(odb, path, fs, "md5")
        odb.add(path, fs, new_obj.hash_info.value, hardlink=False)
        return obj.add(new, meta, new_obj.hash_info)

    if keys not in obj._dict:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
    if op == "test":
        return
    if op == "remove":
        obj._dict.pop(keys)
        obj.__dict__.pop("trie", None)
        return
    if op in ("copy", "move"):
        new = tuple(application["to"].split("/"))
        obj.add(new, *obj.get(keys))
        if op == "move":
            obj._dict.pop(keys)
        return
    raise ValueError(f"unknown {op=}")


def multi_value(*opts, **kwargs):
    return click.option(*opts, multiple=True, required=False, **kwargs)


cl_path = click.Path(
    exists=True, file_okay=True, dir_okay=False, readable=True
)
cl_path_dash = click.Path(
    exists=True, file_okay=True, dir_okay=False, readable=True, allow_dash=True
)


@click.command()
@click.argument("oid")
@click.option("--patch-file", type=cl_path_dash)
@multi_value(
    "--add",
    type=(cl_path, str),
    help="Add file from specified local path to a given path in the tree",
)
@multi_value(
    "--modify",
    type=(cl_path, str),
    help="Modify file with specified local path to a given path in the tree",
)
@multi_value("--move", type=(str, str), help="Move a file in the tree")
@multi_value(
    "--copy", type=(str, str), help="Copy path from a tree to another path"
)
@multi_value("--remove", type=str, help="Remove path from a tree")
@multi_value("--test", type=str, help="Check for the existence of the path")
def update_tree(oid, patch_file, add, modify, move, copy, remove, test):
    """Update tree contents virtually with a patch file in json format.

    Example patch file for reference:

    [\n
        {"op": "remove", "path": "test/0/00004.png"},\n
        {"op": "move", "path": "test/1/00003.png", "to": "test/0/00003.png"},\n
        {"op": "copy", "path": "test/1/00003.png", "to": "test/1/11113.png"},\n
        {"op": "test", "path": "test/1/00003.png"},\n
        {"op": "add", "path": "local/path/to/patch.json", "to": "foo"},\n
        {"op": "modify", "path": "local/path/to/patch.json", "to": "bar"}\n
    ]\n

    Example: ./cli.py update-tree f23d4 patch.json
    """
    odb = get_odb()
    oid = from_shortoid(odb, oid)
    obj = load(odb, odb.get(oid).hash_info)
    assert isinstance(obj, Tree)

    patch = process_patch(
        patch_file,
        add=add,
        remove=remove,
        modify=modify,
        copy=copy,
        move=move,
        test=test,
    )
    for application in patch:
        try:
            apply_op(odb, obj, application)
        except (FileExistsError, FileNotFoundError) as exc:
            typer.echo(exc, err=True, color=True)
            raise typer.Exit(1) from exc

    obj.digest()
    typer.echo(obj)
    odb.add(obj.path, obj.fs, obj.oid, hardlink=True)


@app.command(help="Checkout from the object into a given path")
def checkout(
    oid: str,
    path: Path = typer.Argument(..., resolve_path=True),
    relink: bool = False,
    force: bool = False,
    type: List[LinkEnum] = typer.Option(  # pylint: disable=redefined-builtin
        ["copy"]
    ),
):
    odb = get_odb(type=[t.value for t in type])
    oid = from_shortoid(odb, oid)
    obj = load(odb, odb.get(oid).hash_info)
    with Tqdm(total=len(obj), desc="Checking out", unit="obj") as pbar:
        _checkout(
            os.fspath(path),
            LocalFileSystem(),
            obj,
            odb,
            relink=relink,
            force=force,
            prompt=typer.confirm,
            state=odb.state,
            progress_callback=lambda *_: pbar.update(),
        )


main = typer.main.get_command(app)
main.add_command(update_tree, "update-tree")  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
