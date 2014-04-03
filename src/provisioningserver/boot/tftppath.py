# Copyright 2012-2014 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Construct TFTP paths for boot files."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = [
    'compose_image_path',
    'drill_down',
    'list_boot_images',
    'list_subdirs',
    'locate_tftp_path',
    ]

import errno
from itertools import chain
from logging import getLogger
import os.path


logger = getLogger(__name__)


def compose_image_path(arch, subarch, release, label):
    """Compose the TFTP path for a PXE kernel/initrd directory.

    The path returned is relative to the TFTP root, as it would be
    identified by clients on the network.

    :param arch: Main machine architecture.
    :param subarch: Sub-architecture, or "generic" if there is none.
    :param release: Operating system release, e.g. "precise".
    :param label: Release label, e.g. "release" or "alpha-2".
    :return: Path for the corresponding image directory (containing a
        kernel and initrd) as exposed over TFTP.
    """
    # This is a TFTP path, not a local filesystem path, so hard-code the slash.
    return '/'.join([arch, subarch, release, label])


def locate_tftp_path(path, tftproot):
    """Return the local filesystem path corresponding to `path`.

    The return value gives the filesystem path where you'd have to put
    a file if you wanted it made available over TFTP as `path`.

    :param path: Path as used in the TFTP protocol for which you want the
        local filesystem equivalent. Pass `None` to get the root of the TFTP
        hierarchy.
    :param tftproot: The TFTP root directory.
    """
    if path is None:
        return tftproot
    return os.path.join(tftproot, path.lstrip('/'))


def is_visible_subdir(directory, subdir):
    """Is `subdir` a non-hidden sub-directory of `directory`?"""
    if subdir.startswith('.'):
        return False
    else:
        return os.path.isdir(os.path.join(directory, subdir))


def list_subdirs(directory):
    """Return a list of non-hidden directories in `directory`."""
    return [
        subdir
        for subdir in os.listdir(directory)
        if is_visible_subdir(directory, subdir)
    ]


def extend_path(directory, path):
    """Dig one directory level deeper on `os.path.join(directory, *path)`.

    If `path` is a list of consecutive path elements drilling down from
    `directory`, return a list of sub-directory paths leading one step
    further down.

    :param directory: Base directory that `path` is relative to.
    :param path: A path to a subdirectory of `directory`, represented as
        a list of path elements relative to `directory`.
    :return: A list of paths that go one sub-directory level further
        down from `path`.
    """
    return [
        path + [subdir]
        for subdir in list_subdirs(os.path.join(directory, *path))]


def drill_down(directory, paths):
    """Find the extensions of `paths` one level deeper into the filesystem.

    :param directory: Base directory that each path in `paths` is relative to.
    :param paths: A list of "path lists."  Each path list is a list of
        path elements drilling down into the filesystem from `directory`.
    :return: A list of paths, each of which drills one level deeper down into
        the filesystem hierarchy than the originals in `paths`.
    """
    return list(chain.from_iterable(
        extend_path(directory, path) for path in paths))


def extract_image_params(path):
    """Represent a list of TFTP path elements as a list of boot-image dicts.

    The path must consist of a full [architecture, subarchitecture, release]
    that identify a kind of boot that we may need an image for.
    """
    arch, subarch, release, label = path
    # XXX: rvb 2014-03-24: The images import script currently imports all the
    # images for the configured selections (where a selection is an
    # arch/subarch/series/label combination).  When the import script grows the
    # ability to import the images for a particular purpose, we need to change
    # this code to report what is actually present.
    purposes = ['commissioning', 'install', 'xinstall']
    return [
        dict(
            architecture=arch, subarchitecture=subarch,
            release=release, label=label, purpose=purpose)
        for purpose in purposes
        ]


def list_boot_images(tftproot):
    """List the available boot images.

    :param tftproot: TFTP root directory.
    :return: A list of dicts, describing boot images as consumed by the
        `report_boot_images` API call.
    """
    # The sub-directories directly under tftproot, if they contain
    # images, represent architectures.
    try:
        potential_archs = list_subdirs(tftproot)
    except OSError as exception:
        if exception.errno == errno.ENOENT:
            # Directory does not exist, so return empty list.
            logger.warning("No boot images have been imported yet.")
            return []
        else:
            # Other error. Propagate.
            raise

    # Starting point for iteration: paths that contain only the
    # top-level subdirectory of tftproot, i.e. the architecture name.
    paths = [[subdir] for subdir in potential_archs]

    # Extend paths deeper into the filesystem, through the levels that
    # represent sub-architecture, release, and label.  Any directory
    # that doesn't extend this deep isn't a boot image.
    for level in ['subarch', 'release', 'label']:
        paths = drill_down(tftproot, paths)

    # Each path we find this way should be a boot image.
    # This gets serialised to JSON, so we really have to return a list, not
    # just any iterable.
    return list(chain.from_iterable(
        extract_image_params(path) for path in paths))
