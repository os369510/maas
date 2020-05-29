# Copyright 2017-2019 Canonical Ltd.  This software is licensed under the GNU
# Affero General Public License version 3 (see the file LICENSE).

"""Snap management commands."""

__all__ = [
    "cmd_config",
    "cmd_init",
    "cmd_migrate",
    "cmd_status",
    "cmd_reconfigure_supervisord",
]

import argparse
from collections import OrderedDict
from contextlib import contextmanager
import grp
import os
import pwd
import random
import shutil
import signal
import string
import subprocess
import sys
from textwrap import dedent
import threading
import time

import netifaces
import psycopg2
from psycopg2.extensions import parse_dsn
import tempita

from maascli.command import Command, CommandError
from maascli.configfile import MAASConfiguration
from maascli.init import (
    add_candid_options,
    add_create_admin_options,
    add_rbac_options,
    init_maas,
    print_msg,
    prompt_for_choices,
    read_input,
)


def add_deprecated_mode_argument(parser):
    """Add the --mode argument that is deprecated for 2.8"""
    parser.add_argument(
        "--mode",
        choices=["all", "region+rack", "region", "rack", "none"],
        help=argparse.SUPPRESS,
        dest="deprecated_mode",
    )


ARGUMENTS = OrderedDict(
    [
        (
            "maas-url",
            {
                "help": (
                    "URL that MAAS should use for communicate from the nodes to "
                    "MAAS and other controllers of MAAS."
                ),
                "for_mode": ["region+rack", "region", "rack"],
            },
        ),
        (
            "database-uri",
            {
                "help": (
                    "URI for the MAAS Postgres database in the form of "
                    "postgres://user:pass@host:port/dbname or "
                    "maas-test-db:///. For maas-test-db:/// to work, the "
                    "maas-test-db snap needs to be installed and connected"
                ),
                "for_mode": ["region+rack", "region"],
            },
        ),
        (
            "database-host",
            {"help": argparse.SUPPRESS, "for_mode": ["region+rack", "region"]},
        ),
        (
            "database-port",
            {
                "type": int,
                "help": argparse.SUPPRESS,
                "for_mode": ["region+rack", "region"],
            },
        ),
        (
            "database-name",
            {"help": argparse.SUPPRESS, "for_mode": ["region+rack", "region"]},
        ),
        (
            "database-user",
            {"help": argparse.SUPPRESS, "for_mode": ["region+rack", "region"]},
        ),
        (
            "database-pass",
            {"help": argparse.SUPPRESS, "for_mode": ["region+rack", "region"]},
        ),
        (
            "secret",
            {
                "help": (
                    "Secret token required for the rack controller to talk "
                    "to the region controller(s). Only used when in 'rack' mode."
                ),
                "for_mode": ["rack"],
            },
        ),
        (
            "num-workers",
            {
                "type": int,
                "help": "Number of regiond worker processes to run.",
                "for_mode": ["region+rack", "region"],
            },
        ),
        (
            "enable-debug",
            {
                "action": "store_true",
                "help": (
                    "Enable debug mode for detailed error and log reporting."
                ),
                "for_mode": ["region+rack", "region"],
            },
        ),
        (
            "disable-debug",
            {
                "action": "store_true",
                "help": "Disable debug mode.",
                "for_mode": ["region+rack", "region"],
            },
        ),
        (
            "enable-debug-queries",
            {
                "action": "store_true",
                "help": (
                    "Enable query debugging. Reports number of queries and time for "
                    "all actions performed. Requires debug to also be True. mode for "
                    "detailed error and log reporting."
                ),
                "for_mode": ["region+rack", "region"],
            },
        ),
        (
            "disable-debug-queries",
            {
                "action": "store_true",
                "help": "Disable query debugging.",
                "for_mode": ["region+rack", "region"],
            },
        ),
    ]
)

NON_ROOT_USER = "snap_daemon"


def get_default_gateway_ip():
    """Return the default gateway IP."""
    gateways = netifaces.gateways()
    defaults = gateways.get("default")
    if not defaults:
        return

    def default_ip(family):
        gw_info = defaults.get(family)
        if not gw_info:
            return
        addresses = netifaces.ifaddresses(gw_info[1]).get(family)
        if addresses:
            return addresses[0]["addr"]

    return default_ip(netifaces.AF_INET) or default_ip(netifaces.AF_INET6)


def get_default_url():
    """Return the best default URL for MAAS."""
    gateway_ip = get_default_gateway_ip()
    if not gateway_ip:
        gateway_ip = "localhost"
    return "http://%s:5240/MAAS" % gateway_ip


def get_mode_filepath():
    """Return the path to the 'snap_mode' file."""
    return os.path.join(os.environ["SNAP_COMMON"], "snap_mode")


def get_current_mode():
    """Gets the current mode of the snap."""
    filepath = get_mode_filepath()
    if os.path.exists(filepath):
        with open(get_mode_filepath(), "r") as fp:
            return fp.read().strip()
    else:
        return "none"


def get_base_db_dir():
    """Return the base dir for postgres."""
    return os.path.join(os.environ["SNAP_COMMON"], "postgres")


def set_current_mode(mode):
    """Set the current mode of the snap."""
    with open(get_mode_filepath(), "w") as fp:
        fp.write(mode.strip())


def render_supervisord(mode):
    """Render the 'supervisord.conf' based on the mode."""
    conf_vars = {"postgresql": False, "regiond": False, "rackd": False}
    if mode == "all":
        conf_vars["postgresql"] = True
    if mode in ["all", "region+rack", "region"]:
        conf_vars["regiond"] = True
    if mode in ["all", "region+rack", "rack"]:
        conf_vars["rackd"] = True
    template = tempita.Template.from_filename(
        os.path.join(
            os.environ["SNAP"],
            "usr",
            "share",
            "maas",
            "supervisord.conf.template",
        ),
        encoding="UTF-8",
    )
    rendered = template.substitute(conf_vars)
    conf_path = os.path.join(
        os.environ["SNAP_DATA"], "supervisord", "supervisord.conf"
    )
    with open(conf_path, "w") as fp:
        fp.write(rendered)


def get_supervisord_pid():
    """Get the running supervisord pid."""
    pid_path = os.path.join(
        os.environ["SNAP_DATA"], "supervisord", "supervisord.pid"
    )
    if os.path.exists(pid_path):
        with open(pid_path, "r") as fp:
            return int(fp.read().strip())
    else:
        return None


def sighup_supervisord():
    """Cause supervisord to stop all processes, reload configuration, and
    start all processes."""
    pid = get_supervisord_pid()
    if pid is None:
        return

    try:
        os.kill(pid, signal.SIGHUP)
    except ProcessLookupError:
        return

    # Wait for supervisord to be running successfully.
    time.sleep(0.5)
    while True:
        process = subprocess.Popen(
            [
                os.path.join(os.environ["SNAP"], "bin", "run-supervisorctl"),
                "status",
            ],
            stdout=subprocess.PIPE,
        )
        process.wait()
        output = process.stdout.read().decode("utf-8")
        # Error message is printed until supervisord is running correctly.
        if "error:" in output:
            time.sleep(1)
        else:
            break


def print_config_value(config, key, hidden=False):
    """Print the configuration value to stdout."""
    template = "{key}=(hidden)" if hidden else "{key}={value}"
    print_msg(template.format(key=key, value=config.get(key)))


def get_rpc_secret():
    """Get the current RPC secret."""
    secret = None
    secret_path = os.path.join(
        os.environ["SNAP_DATA"], "var", "lib", "maas", "secret"
    )
    if os.path.exists(secret_path):
        with open(secret_path, "r") as fp:
            secret = fp.read().strip()
    if secret:
        return secret
    else:
        return None


def set_rpc_secret(secret):
    """Write/delete the RPC secret."""
    secret_path = os.path.join(
        os.environ["SNAP_DATA"], "var", "lib", "maas", "secret"
    )
    if secret:
        # Write the secret.
        with open(secret_path, "w") as fp:
            fp.write(secret)
    else:
        # Delete the secret.
        if os.path.exists(secret_path):
            os.remove(secret_path)


def print_config(
    parsable=False, show_database_password=False, show_secret=False
):
    """Print the config output."""
    current_mode = get_current_mode()
    config = MAASConfiguration().get()
    if parsable:
        print_msg("mode=%s" % current_mode)
    else:
        print_msg("Mode: %s" % current_mode)
    if current_mode != "none":
        if not parsable:
            print_msg("Settings:")
        print_config_value(config, "maas_url")
        if current_mode in ["region+rack", "region"]:
            print_config_value(config, "database_host")
            print_config_value(config, "database_port")
            print_config_value(config, "database_name")
            print_config_value(config, "database_user")
            print_config_value(
                config, "database_pass", hidden=(not show_database_password)
            )
        if current_mode == "rack":
            secret = "(hidden)"
            if show_secret:
                secret = get_rpc_secret()
            print_msg("secret=%s" % secret)
        if current_mode != "rack":
            if "num_workers" in config:
                print_config_value(config, "num_workers")
            if "debug" in config:
                print_config_value(config, "debug")
            if "debug_queries" in config:
                print_config_value(config, "debug_queries")


def change_user(username, effective=False):
    """Change running user, by default to the non-root user."""
    running_uid = pwd.getpwnam(username).pw_uid
    running_gid = grp.getgrnam(username).gr_gid
    os.setgroups([])
    if effective:
        os.setegid(running_gid)
        os.seteuid(running_uid)
    else:
        os.setgid(running_gid)
        os.setuid(running_uid)


@contextmanager
def privileges_dropped():
    """Context manager to run things as non-root user."""
    change_user(NON_ROOT_USER, effective=True)
    yield
    change_user("root", effective=True)


def run_with_drop_privileges(cmd, *args, **kwargs):
    """Runs `cmd` in child process with lower privileges."""
    pid = os.fork()

    if pid == 0:
        change_user(NON_ROOT_USER)
        cmd(*args, **kwargs)
        sys.exit(0)
    else:

        def signal_handler(signal, frame):
            with privileges_dropped():
                os.kill(pid, signal)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        _, code = os.waitpid(pid, 0)
        if code:
            # fail if the child process failed (the error will be reported
            # there)
            sys.exit(1)


def run_sql(sql):
    """Run sql command through `psql`."""
    subprocess.check_output(
        [
            os.path.join(os.environ["SNAP"], "bin", "psql"),
            "-h",
            os.path.join(get_base_db_dir(), "sockets"),
            "-d",
            "postgres",
            "-U",
            "postgres",
            "-c",
            sql,
        ],
        stderr=subprocess.STDOUT,
    )


def wait_for_postgresql(timeout=60):
    """Wait for postgresql to be running."""
    end_time = time.time() + timeout
    while True:
        try:
            run_sql("SELECT now();")
        except subprocess.CalledProcessError:
            if time.time() > end_time:
                raise TimeoutError(
                    "Unable to connect to postgresql after %s seconds."
                    % timeout
                )
            else:
                time.sleep(1)
        else:
            break


def start_postgres():
    """Start postgresql."""
    base_db_dir = get_base_db_dir()
    subprocess.check_output(
        [
            os.path.join(os.environ["SNAP"], "bin", "pg_ctl"),
            "start",
            "-w",
            "-D",
            os.path.join(base_db_dir, "data"),
            "-l",
            os.path.join(
                os.environ["SNAP_COMMON"], "log", "postgresql-init.log"
            ),
            "-o",
            '-k "%s" -h ""' % os.path.join(base_db_dir, "sockets"),
        ],
        stderr=subprocess.STDOUT,
    )
    wait_for_postgresql()


def stop_postgres():
    """Stop postgresql."""
    subprocess.check_output(
        [
            os.path.join(os.environ["SNAP"], "bin", "pg_ctl"),
            "stop",
            "-w",
            "-D",
            os.path.join(get_base_db_dir(), "data"),
        ],
        stderr=subprocess.STDOUT,
    )


def create_db(config):
    """Create the database and user."""
    run_sql(
        "CREATE USER %s WITH PASSWORD '%s';"
        % (config["database_user"], config["database_pass"])
    )
    run_sql("CREATE DATABASE %s;" % config["database_name"])
    run_sql(
        "GRANT ALL PRIVILEGES ON DATABASE %s to %s;"
        % (config["database_name"], config["database_user"])
    )


@contextmanager
def with_postgresql():
    """Start or stop postgresql."""
    start_postgres()
    yield
    stop_postgres()


def migrate_db(capture=False):
    """Migrate the database."""
    if capture:
        process = subprocess.Popen(
            [
                os.path.join(os.environ["SNAP"], "bin", "maas-region"),
                "dbupgrade",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        ret = process.wait()
        output = process.stdout.read().decode("utf-8")
        if ret != 0:
            clear_line()
            print_msg("Failed to perfom migrations:")
            print_msg(output)
            print_msg("")
            sys.exit(ret)
    else:
        subprocess.check_call(
            [
                os.path.join(os.environ["SNAP"], "bin", "maas-region"),
                "dbupgrade",
            ]
        )


def init_db():
    """Initialize the database."""
    config_data = MAASConfiguration().get()
    base_db_dir = get_base_db_dir()
    db_path = os.path.join(base_db_dir, "data")
    if not os.path.exists(base_db_dir):
        os.mkdir(base_db_dir)
        # allow both root and non-root user to create/delete directories under
        # this one
        shutil.chown(base_db_dir, group=NON_ROOT_USER)
        os.chmod(base_db_dir, 0o770)
    if os.path.exists(db_path):
        run_with_drop_privileges(shutil.rmtree, db_path)
    os.mkdir(db_path)
    shutil.chown(db_path, user=NON_ROOT_USER, group=NON_ROOT_USER)
    log_path = os.path.join(
        os.environ["SNAP_COMMON"], "log", "postgresql-init.log"
    )
    if not os.path.exists(log_path):
        open(log_path, "a").close()
    shutil.chown(log_path, user=NON_ROOT_USER, group=NON_ROOT_USER)
    socket_path = os.path.join(get_base_db_dir(), "sockets")
    if not os.path.exists(socket_path):
        os.mkdir(socket_path)
        os.chmod(socket_path, 0o775)
        shutil.chown(socket_path, user=NON_ROOT_USER)  # keep root as group

    def _init_db():
        subprocess.check_output(
            [
                os.path.join(os.environ["SNAP"], "bin", "initdb"),
                "-D",
                os.path.join(get_base_db_dir(), "data"),
                "-U",
                "postgres",
                "-E",
                "UTF8",
                "--locale=C",
            ],
            stderr=subprocess.STDOUT,
        )
        with with_postgresql():
            create_db(config_data)

    run_with_drop_privileges(_init_db)


def clear_line():
    """Resets the current line when in a terminal."""
    if sys.stdout.isatty():
        print_msg(
            "\r" + " " * int(os.environ.get("COLUMNS", 0)), newline=False
        )


def perform_work(msg, cmd, *args, **kwargs):
    """Perform work.

    Executes the `cmd` and while its running it prints a nice message.
    """
    # When not running in a terminal, just print the message once and perform
    # the operation.
    if not sys.stdout.isatty():
        print_msg(msg)
        return cmd(*args, **kwargs)

    spinner = {
        0: "/",
        1: "-",
        2: "\\",
        3: "|",
        4: "/",
        5: "-",
        6: "\\",
        7: "|",
    }

    def _write_msg(evnt):
        idx = 0
        while not evnt.is_set():
            # Print the message with a spinner until the work is complete.
            print_msg("\r[%s] %s" % (spinner[idx], msg), newline=False)
            idx += 1
            if idx == 8:
                idx = 0
            time.sleep(0.25)
        # Clear the line so previous message is not show if the next message
        # is not as long as this message.
        print_msg("\r" + " " * (len(msg) + 4), newline=False)

    # Spawn a thread to print the message, while performing the work in the
    # current execution thread.
    evnt = threading.Event()
    t = threading.Thread(target=_write_msg, args=(evnt,))
    t.start()
    try:
        ret = cmd(*args, **kwargs)
    finally:
        evnt.set()
        t.join()
    clear_line()
    return ret


def required_prompt(title, help_text=None, default=None):
    """Prompt for required input."""
    value = None
    if default is not None:
        default_text = f" [default={default}]"
    else:
        default_text = ""
    prompt = f"{title}{default_text}: "
    while not value or value == "help":
        value = read_input(prompt)
        if not value and default is not None:
            value = default

        if value == "help":
            if help_text:
                print_msg(help_text)
    return value


class SnappyCommand(Command):
    """
    Command that just prints the exception instead of the overridden
    'maas --help' output.
    """

    def __call__(self, options):
        try:
            self.handle(options)
        except Exception as exc:
            exc.always_show = True
            raise exc


def monkey_patch_for_all_mode_bw_compatability(parser):
    """Allow 'maas init --mode=$mode' to keep working for 2.8.

    If --mode=$mode is specified, prepend the appropriate sub command to
    the arg list.

    --mode=$mode is deprecated and will be removed in 2.9.
    """

    def _parse_known_args(arg_strings, namespace):
        mode_parser = argparse.ArgumentParser(add_help=False)
        add_deprecated_mode_argument(mode_parser)
        options, rest = mode_parser.parse_known_args(arg_strings)
        if options.deprecated_mode is not None:
            if options.deprecated_mode == "all":
                print_msg(
                    "\nWARNING: Passing --mode=all is deprecated and "
                    "will be removed in the 2.9 release.\n"
                    "See https://maas.io/deprecations/MD1 for more details.\n",
                    stderr=True,
                )
            sub_command = (
                "region+rack"
                if options.deprecated_mode in ["all", "none"]
                else options.deprecated_mode
            )
            arg_strings = [
                f"--mode={options.deprecated_mode}",
                sub_command,
            ] + rest
        return real_parse_known_args(arg_strings, namespace)

    real_parse_known_args = parser._parse_known_args
    parser._parse_known_args = _parse_known_args


def using_deprecated_database_options(options):
    return (
        options.database_host is not None
        or options.database_name is not None
        or options.database_user is not None
        or options.database_pass is not None
        or options.database_port is not None
    )


class DatabaseSettingsError(Exception):
    """Something was wrong with the database settings."""


MAAS_TEST_DB_URI = "maas-test-db:///"


def get_database_settings(options):
    """Get the database setting to use.

    If some of the deprecated --database-foo options were used, it
    prompts for the missing data.

    Else, it will either read --database-uri from the options, or prompt
    for it. Whem prompting for it, it will default to the maas-test-db
    URI if the maas-test-db snap is installed and connected.
    """

    if using_deprecated_database_options(options):
        if options.database_uri:
            raise DatabaseSettingsError(
                "Can't use deprecated --database-* parameters together with "
                "--database-uri"
            )
        if using_deprecated_database_options(options):
            print_msg(
                "\nWARNING: Passing individual database configs is deprecated "
                "and will be removed in the 2.9 release.\n"
                "Please use --database-uri instead.\n",
                stderr=True,
            )
        database_host = options.database_host
        if not database_host:
            database_host = required_prompt(
                "Database host", help_text=ARGUMENTS["database-host"]["help"]
            )
        database_name = options.database_name
        if not database_name:
            database_name = required_prompt(
                "Database name", help_text=ARGUMENTS["database-name"]["help"]
            )
        database_user = options.database_user
        if not database_user:
            database_user = required_prompt(
                "Database user", help_text=ARGUMENTS["database-user"]["help"]
            )
        database_pass = options.database_pass
        if not database_pass:
            database_pass = required_prompt(
                "Database password",
                help_text=ARGUMENTS["database-pass"]["help"],
            )
        database_settings = {
            "database_host": database_host,
            "database_name": database_name,
            "database_user": database_user,
            "database_pass": database_pass,
        }
        # Add the port to the configuration if exists. By default
        # MAAS handles picking the port automatically in the backend
        # if none provided.
        if options.database_port is not None:
            database_settings["database_port"] = options.database_port
    else:
        database_uri = options.database_uri
        test_db_socket = os.path.join(
            os.environ["SNAP_COMMON"], "test-db-socket"
        )
        test_db_uri = f"postgres:///maasdb?host={test_db_socket}&user=maas"
        if database_uri is None:
            default_uri = None
            if os.path.exists(test_db_socket):
                default_uri = MAAS_TEST_DB_URI
            database_uri = required_prompt(
                f"Database URI",
                default=default_uri,
                help_text=ARGUMENTS["database-uri"]["help"],
            )
            if not database_uri:
                database_uri = test_db_uri
        # parse_dsn gives very confusing error messages if you pass in
        # an invalid URI, so let's make sure the URI is of the form
        # postgres://... before calling parse_dsn.
        if database_uri != MAAS_TEST_DB_URI and not database_uri.startswith(
            "postgres://"
        ):
            raise DatabaseSettingsError(
                f"Database URI needs to be either '{MAAS_TEST_DB_URI}' or "
                "start with 'postgres://'"
            )
        if database_uri == MAAS_TEST_DB_URI:
            database_uri = test_db_uri
        try:
            parsed_dsn = parse_dsn(database_uri)
        except psycopg2.ProgrammingError as error:
            raise DatabaseSettingsError(
                "Error parsing database URI: " + str(error).strip()
            )
        unsupported_params = set(parsed_dsn.keys()).difference(
            ["user", "password", "host", "dbname", "port"]
        )
        if unsupported_params:
            raise DatabaseSettingsError(
                "Error parsing database URI: Unsupported parameters: "
                + ", ".join(sorted(unsupported_params))
            )
        if "user" not in parsed_dsn:
            raise DatabaseSettingsError(
                f"No user found in URI: {database_uri}"
            )
        if "host" not in parsed_dsn:
            parsed_dsn["host"] = "localhost"
        if "dbname" not in parsed_dsn:
            parsed_dsn["dbname"] = parsed_dsn["user"]
        database_settings = {
            "database_host": parsed_dsn["host"],
            "database_name": parsed_dsn["dbname"],
            "database_user": parsed_dsn.get("user", ""),
            "database_pass": parsed_dsn.get("password"),
        }
        if "port" in parsed_dsn:
            database_settings["database_port"] = int(parsed_dsn["port"])
    return database_settings


class cmd_init(SnappyCommand):
    """Initialise MAAS in the specified run mode.

    When installing region or rack+region modes, MAAS needs a
    PostgreSQL database to connect to.

    If you want to set up PostgreSQL for a non-production deployment on
    this machine, and configure it for use with MAAS, you can install
    the maas-test-db snap before running 'maas init':

        sudo snap install maas-test-db
        sudo maas init region+rack --database-uri maas-test-db:///

    """

    def __init__(self, parser):
        super(cmd_init, self).__init__(parser)
        monkey_patch_for_all_mode_bw_compatability(parser)
        subparsers = parser.add_subparsers(
            metavar=None, title="run modes", dest="run_mode"
        )
        subparsers.required = True
        subparsers_map = {}
        subparsers_map["region+rack"] = subparsers.add_parser(
            "region+rack",
            help="Both region and rack controllers",
            description=(
                "Initialise MAAS to run both a region and rack controller."
            ),
        )
        subparsers_map["region"] = subparsers.add_parser(
            "region",
            help="Region controller only",
            description=("Initialise MAAS to run only a region controller."),
        )
        subparsers_map["rack"] = subparsers.add_parser(
            "rack",
            help="Rack controller only",
            description=("Initialise MAAS to run only a rack controller."),
        )
        for argument, kwargs in ARGUMENTS.items():
            kwargs = kwargs.copy()
            for_modes = kwargs.pop("for_mode")
            for for_mode in for_modes:
                subparsers_map[for_mode].add_argument(
                    "--%s" % argument, **kwargs
                )

        add_deprecated_mode_argument(parser)
        for for_mode in ["region+rack", "region", "rack"]:
            subparsers_map[for_mode].add_argument(
                "--force",
                action="store_true",
                help=(
                    "Skip confirmation questions when initialization has "
                    "already been performed."
                ),
            )
        parser.add_argument(
            "--enable-candid",
            default=False,
            action="store_true",
            help=argparse.SUPPRESS,
        )
        for for_mode in ["region+rack", "region"]:
            add_candid_options(subparsers_map[for_mode], suppress_help=True)
            add_rbac_options(subparsers_map[for_mode], suppress_help=True)
            subparsers_map[for_mode].add_argument(
                "--skip-admin", action="store_true", help=argparse.SUPPRESS
            )
            add_create_admin_options(
                subparsers_map[for_mode], suppress_help=True
            )

    def handle(self, options):
        if os.getuid() != 0:
            raise SystemExit("The 'init' command must be run by root.")

        mode = options.run_mode
        if options.deprecated_mode:
            mode = options.deprecated_mode
        current_mode = get_current_mode()
        if current_mode != "none":
            if not options.force:
                init_text = "initialize again"
                if mode == "none":
                    init_text = "de-initialize"
                else:
                    print_msg("Controller has already been initialized.")
                initialize = prompt_for_choices(
                    "Are you sure you want to %s "
                    "(yes/no) [default=no]? " % init_text,
                    ["yes", "no"],
                    default="no",
                )
                if initialize == "no":
                    sys.exit(0)

        if current_mode == "all" and mode != "all" and not options.force:
            print_msg(
                "This will disconnect your MAAS from the running database."
            )
            disconnect = prompt_for_choices(
                "Are you sure you want to disconnect the database "
                "(yes/no) [default=no]? ",
                ["yes", "no"],
                default="no",
            )
            if disconnect == "no":
                return 0
        elif current_mode == "all" and mode == "all" and not options.force:
            print_msg(
                "This will re-initialize your entire database and all "
                "current data will be lost."
            )
            reinit_db = prompt_for_choices(
                "Are you sure you want to re-initialize the database "
                "(yes/no) [default=no]? ",
                ["yes", "no"],
                default="no",
            )
            if reinit_db == "no":
                return 0

        rpc_secret = None
        if mode == "all":
            database_settings = {
                "database_host": os.path.join(get_base_db_dir(), "sockets"),
                "database_name": "maasdb",
                "database_user": "maas",
                "database_pass": "".join(
                    random.choice(string.ascii_uppercase + string.digits)
                    for _ in range(10)
                ),
            }
        elif mode in ["region", "region+rack"]:
            try:
                database_settings = get_database_settings(options)
            except DatabaseSettingsError as error:
                raise CommandError(str(error))
        else:
            database_settings = {}
        maas_url = options.maas_url
        if mode != "none" and not maas_url:
            maas_url = required_prompt(
                "MAAS URL",
                default=get_default_url(),
                help_text=ARGUMENTS["maas-url"]["help"],
            )
        if mode == "rack":
            rpc_secret = options.secret
            if not rpc_secret:
                rpc_secret = required_prompt(
                    "Secret", help_text=ARGUMENTS["secret"]["help"]
                )

        # Stop all services if in another mode.
        if current_mode != "none":

            def stop_services():
                render_supervisord("none")
                sighup_supervisord()

            perform_work("Stopping services", stop_services)

        # Configure the settings.
        settings = {"maas_url": maas_url}
        settings.update(database_settings)

        MAASConfiguration().update(settings)
        set_rpc_secret(rpc_secret)

        # Finalize the Initialization.
        self._finalize_init(mode, options)

    def _finalize_init(self, mode, options):
        # When in 'all' mode configure the database.
        if mode == "all":
            perform_work("Initializing database", init_db)

        # Configure mode.
        def start_services():
            render_supervisord(mode)
            set_current_mode(mode)
            sighup_supervisord()

        perform_work(
            "Starting services" if mode != "none" else "Stopping services",
            start_services,
        )

        if mode == "all":
            # When in 'all' mode configure the database and create admin user.
            perform_work("Waiting for postgresql", wait_for_postgresql)
            perform_work(
                "Performing database migrations",
                migrate_db,
                capture=sys.stdout.isatty(),
            )
            init_maas(options)
        elif mode in ["region", "region+rack"]:
            # When in 'region' or 'region+rack' the migrations for the database
            # must be at the same level as this controller.
            perform_work(
                "Performing database migrations",
                migrate_db,
                capture=sys.stdout.isatty(),
            )
            print_msg(
                dedent(
                    """\
                    MAAS has been set up.

                    If you want to configure external authentication or use
                    MAAS with Canonical RBAC, please run

                      sudo maas configauth

                    To create admins when not using external authentication, run

                      sudo maas createadmin
                    """
                )
            )


class cmd_config(SnappyCommand):
    """View or change controller configuration."""

    # Required options based on mode.
    required_options = {
        "region+rack": [
            "maas_url",
            "database_host",
            "database_name",
            "database_user",
            "database_pass",
        ],
        "region": [
            "maas_url",
            "database_host",
            "database_name",
            "database_user",
            "database_pass",
        ],
        "rack": ["maas_url", "secret"],
        "none": [],
    }

    # Required flags that are in .conf.
    setting_flags = (
        "maas_url",
        "database_host",
        "database_name",
        "database_user",
        "database_pass",
    )

    # Optional flags that are in .conf.
    optional_flags = {
        "num_workers": {"type": "int", "config": "num_workers"},
        "enable_debug": {
            "type": "store_true",
            "set_value": True,
            "config": "debug",
        },
        "disable_debug": {
            "type": "store_true",
            "set_value": False,
            "config": "debug",
        },
        "enable_debug_queries": {
            "type": "store_true",
            "set_value": True,
            "config": "debug_queries",
        },
        "disable_debug_queries": {
            "type": "store_true",
            "set_value": False,
            "config": "debug_queries",
        },
    }

    def __init__(self, parser):
        super(cmd_config, self).__init__(parser)
        parser.add_argument(
            "--show",
            action="store_true",
            help=(
                "Show the current configuration. Default when no parameters "
                "are provided."
            ),
        )
        parser.add_argument(
            "--show-database-password",
            action="store_true",
            help="Show the hidden database password.",
        )
        parser.add_argument(
            "--show-secret",
            action="store_true",
            help="Show the hidden secret.",
        )
        for argument, kwargs in ARGUMENTS.items():
            if argument == "database-uri":
                # 'maas config' doesn't support database-uri, since it's
                # more of a low-level tool for changing the MAAS
                # configuration directly.
                continue
            kwargs = kwargs.copy()
            kwargs.pop("for_mode")
            parser.add_argument("--%s" % argument, **kwargs)
        parser.add_argument(
            "--parsable",
            action="store_true",
            help="Output the current configuration in a parsable format.",
        )
        parser.add_argument(
            "--render", action="store_true", help=argparse.SUPPRESS
        )

    def _validate_flags(self, options, running_mode):
        """
        Validate the flags are correct for the current mode or the new mode.
        """
        invalid_flags = []
        for flag in self.setting_flags + ("secret",):
            if flag not in self.required_options[running_mode] and getattr(
                options, flag
            ):
                invalid_flags.append("--%s" % flag.replace("_", "-"))
        if len(invalid_flags) > 0:
            print_msg(
                "Following flags are not supported in '%s' mode: %s"
                % (running_mode, ", ".join(invalid_flags))
            )
            sys.exit(1)

    def handle(self, options):
        if os.getuid() != 0:
            raise SystemExit("The 'config' command must be run by root.")

        config_manager = MAASConfiguration()

        # Hidden option only called by the run-supervisord script. Renders
        # the initial supervisord.conf based on the current mode.
        if options.render:
            render_supervisord(get_current_mode())
            return

        # In config mode if --show is passed or none of the following flags
        # have been passed.
        in_config_mode = options.show
        if not in_config_mode:
            in_config_mode = not any(
                (
                    getattr(options, flag) is not None
                    and getattr(options, flag) is not False
                )
                for flag in (
                    ("secret",)
                    + self.setting_flags
                    + tuple(self.optional_flags.keys())
                )
            )

        # Config mode returns the current config of the snap.
        if in_config_mode:
            return print_config(
                options.parsable,
                options.show_database_password,
                options.show_secret,
            )
        else:
            restart_required = False
            running_mode = get_current_mode()

            # Validate the mode and flags.
            self._validate_flags(options, running_mode)

            current_config = config_manager.get()
            # Only update the passed settings.
            for flag in self.setting_flags:
                flag_value = getattr(options, flag)
                should_update = (
                    flag_value is not None
                    and current_config.get(flag) != flag_value
                )
                if should_update:
                    config_manager.update({flag: flag_value})
                    restart_required = True
            if options.secret is not None:
                set_rpc_secret(options.secret)

            # fetch config again, as it might have changed
            current_config = config_manager.get()

            # Update any optional settings.
            for flag, flag_info in self.optional_flags.items():
                flag_value = getattr(options, flag)
                if flag_info["type"] != "store_true":
                    flag_key = flag_info["config"]
                    should_update = (
                        flag_value is not None
                        and current_config.get(flag_key) != flag_value
                    )
                    if should_update:
                        config_manager.update({flag_key: flag_value})
                        restart_required = True
                elif flag_value:
                    flag_key = flag_info["config"]
                    flag_value = flag_info["set_value"]
                    if current_config.get(flag_key) != flag_value:
                        config_manager.update({flag_key: flag_value})
                        restart_required = True

            # Restart the supervisor as its required.
            if restart_required:
                perform_work(
                    "Restarting services"
                    if running_mode != "none"
                    else "Stopping services",
                    sighup_supervisord,
                )


class cmd_status(SnappyCommand):
    """Status of controller services."""

    def handle(self, options):
        if os.getuid() != 0:
            raise SystemExit("The 'status' command must be run by root.")

        if get_current_mode() == "none":
            print_msg("MAAS is not configured")
            sys.exit(1)
        else:
            process = subprocess.Popen(
                [
                    os.path.join(
                        os.environ["SNAP"], "bin", "run-supervisorctl"
                    ),
                    "status",
                ],
                stdout=subprocess.PIPE,
            )
            ret = process.wait()
            output = process.stdout.read().decode("utf-8")
            if ret == 0:
                print_msg(output, newline=False)
            else:
                if "error:" in output:
                    print_msg(
                        "MAAS supervisor is currently restarting. "
                        "Please wait and try again."
                    )
                    sys.exit(-1)
                else:
                    print_msg(output, newline=False)
                    sys.exit(ret)


class cmd_migrate(SnappyCommand):
    """Perform migrations on connected database."""

    def __init__(self, parser):
        super(cmd_migrate, self).__init__(parser)
        # '--configure' is hidden and only called from snap hooks to update the
        # database when running in "all" mode
        parser.add_argument(
            "--configure", action="store_true", help=argparse.SUPPRESS
        )

    def handle(self, options):
        if os.getuid() != 0:
            raise SystemExit("The 'migrate' command must be run by root.")

        current_mode = get_current_mode()
        if options.configure:
            if current_mode == "all":
                wait_for_postgresql()
                sys.exit(migrate_db())
            elif current_mode in ["region", "region+rack"]:
                sys.exit(migrate_db())
            else:
                # In 'rack' or 'none' mode, nothing to do.
                sys.exit(0)

        if current_mode == "none":
            print_msg("MAAS is not configured")
            sys.exit(1)
        elif current_mode == "rack":
            print_msg(
                "Mode 'rack' is not connected to a database. "
                "No migrations to perform."
            )
            sys.exit(1)
        else:
            sys.exit(migrate_db())


class cmd_reconfigure_supervisord(SnappyCommand):
    """Rewrite supervisord configuration and signal it to reload."""

    hidden = True

    def handle(self, options):
        render_supervisord(get_current_mode())
        sighup_supervisord()
