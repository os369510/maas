# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Utilities for working with OpenSSH keys."""

__all__ = [
    "normalise_openssh_public_key",
    "OpenSSHKeyError",
]

from itertools import chain
import os
from pathlib import Path
import pipes
from subprocess import (
    CalledProcessError,
    check_output,
    PIPE,
)
from tempfile import TemporaryDirectory

from provisioningserver.utils.shell import select_c_utf8_locale


OPENSSH_PROTOCOL2_KEY_TYPES = frozenset((
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-dss",
    "ssh-ed25519",
    "ssh-rsa",
))


class OpenSSHKeyError(ValueError):
    """The given key was not recognised or was corrupt."""


def normalise_openssh_public_key(keytext):
    """Validate and normalise an OpenSSH public key.

    Essentially: ensure we have a public key first (and not try to extract a
    public key from a private key) and then pump it through an ssh-keygen(1)
    pipeline to ensure it's valid.

    sshd(8) has a section describing the format of ~/.ssh/authorized_keys:

      Each line of the file contains one key (empty lines and lines starting
      with a ‘#’ are ignored as comments). Protocol 1 public keys consist of
      the following space-separated fields: options, bits, exponent, modulus,
      comment. Protocol 2 public key consist of: options, keytype,
      base64-encoded key, comment. The options field is optional; [...]. The
      bits, exponent, modulus, and comment fields give the RSA key for
      protocol version 1; the comment field is not used for anything (but may
      be convenient for the user to identify the key). For protocol version 2
      the keytype is “ecdsa-sha2-nistp256”, “ecdsa-sha2-nistp384”,
      “ecdsa-sha2-nistp521”, “ssh-ed25519”, “ssh-dss” or “ssh-rsa”.

    ssh-keygen(1) explicitly recommends appending public key files to
    ~/.ssh/authorized_keys:

      The contents ... should be added to ~/.ssh/authorized_keys on all
      machines where the user wishes to log in using public key
      authentication.

    Marrying the two we have official documentation for the format of public
    key files!

    We should ignore protocol 1 keys. It does not even appear to be possible
    to create an rsa1 key on Xenial:

      $ ssh-keygen -t rsa1
      Generating public/private rsa1 key pair.
      Enter file in which to save the key (.../.ssh/identity):
      Enter passphrase (empty for no passphrase):
      Enter same passphrase again:
      Saving key ".../.ssh/identity" failed: unknown or unsupported key type

    Although ~/.ssh/authorized_keys can contain options, we should assume that
    the public keys pasted into MAAS do not have options. Public key files
    generated by ssh-keygen(1) will not contain options.

    Given all that, this function does the following:

    1. Checks there are 2 or more fields: keytype base64-encoded-key [comment]
    (the comment can contain whitespace).

    2. Checks that keytype is one of “ssh-dss”, “ssh-rsa”, “ssh-ed25519”,
    “ecdsa-sha2-nistp256”, “ecdsa-sha2-nistp384”, or “ecdsa-sha2-nistp521”,

    2. Run through `setsid -w ssh-keygen -e -f $keyfile > $intermediate <&-`.

    3. Run through `setsid -w ssh-keygen -i -f $intermediate > $pubkey <&-`.

    Note: setsid and <&- ensures ssh-keygen doesn't use the caller's TTY. This
    is Python, and no recourse to a shell is being taken, but it has similar
    behaviour.

    4. $pubkey should contain two fields: keytype, base64-encoded key.

    5. Reunite $pubkey with comment, if there was one.

    Errors from ssh-keygen(1) at any point should be reported *with the error
    message*. Previously all errors relating to SSH keys were coalesced into
    the same static message.

    """
    parts = keytext.split()
    if len(parts) >= 2:
        keytype, key, *comments = parts
    else:
        raise OpenSSHKeyError(
            "Key should contain 2 or more space separated parts (key type, "
            "base64-encoded key, optional comments), not %d: %s" % (
                len(parts), " ".join(map(pipes.quote, parts))))

    if keytype not in OPENSSH_PROTOCOL2_KEY_TYPES:
        raise OpenSSHKeyError(
            "Key type %s not recognised; it should be one of: %s" % (
                pipes.quote(keytype), " ".join(
                    sorted(OPENSSH_PROTOCOL2_KEY_TYPES))))

    env = select_c_utf8_locale()
    # Request OpenSSH to use /bin/true when prompting for passwords. We also
    # have to redirect stdin from, say, /dev/null so that it doesn't use the
    # terminal (when this is executed from a terminal).
    env["SSH_ASKPASS"] = "/bin/true"

    with TemporaryDirectory(prefix="maas") as tempdir:
        keypath = Path(tempdir).joinpath("intermediate")
        # Ensure that this file is locked-down.
        keypath.touch()
        keypath.chmod(0o600)
        # Convert given key to RFC4716 form.
        keypath.write_text("%s %s" % (keytype, key), "utf-8")
        try:
            with open(os.devnull, "r") as devnull:
                rfc4716key = check_output(
                    ("setsid", "-w", "ssh-keygen", "-e", "-f", str(keypath)),
                    stdin=devnull, stderr=PIPE, env=env)
        except CalledProcessError:
            raise OpenSSHKeyError(
                "Key could not be converted to RFC4716 form.")
        # Convert RFC4716 back to OpenSSH format public key.
        keypath.write_bytes(rfc4716key)
        try:
            with open(os.devnull, "r") as devnull:
                opensshkey = check_output(
                    ("setsid", "-w", "ssh-keygen", "-i", "-f", str(keypath)),
                    stdin=devnull, stderr=PIPE, env=env)
        except CalledProcessError:
            # If this happens it /might/ be an OpenSSH bug. If we've managed
            # to convert to RFC4716 form then it seems reasonable to assume
            # that OpenSSH has already given this key its blessing.
            raise OpenSSHKeyError(
                "Key could not be converted from RFC4716 form to "
                "OpenSSH public key form.")
        else:
            keytype, key = opensshkey.decode("utf-8").split()

    return " ".join(chain((keytype, key), comments))
