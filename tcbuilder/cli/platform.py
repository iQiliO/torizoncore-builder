"""
CLI handling for platform subcommand
"""

import argparse
import logging
import os
import shutil

from datetime import datetime, timezone

import dateutil.parser

from tcbuilder.backend import platform, sotaops, common
from tcbuilder.backend.platform import JSON_EXT, OFFLINE_SNAPSHOT_FILE
from tcbuilder.errors import (PathNotExistError, InvalidStateError,
                              InvalidDataError, InvalidArgumentError,
                              TorizonCoreBuilderError)
from tcbuilder.backend.registryops import RegistryOperations

log = logging.getLogger("torizon." + __name__)

IMAGES_DIR = "images/"
DIRECTOR_DIR = "metadata/director/"
IMAGEREPO_DIR = "metadata/image-repo/"
DOCKERMETA_DIR = "metadata/docker/"

DEFAULT_PLATFORMS = ["linux/arm/v7", "linux/arm64"]


def l1_pref(orgstr):
    """Add L1_PREF prefix to orgstr"""
    return "\n=>> " + orgstr


def validate_offupd_metadata(offupd_targets_info, offupd_snapshot_info):
    """Perform validations on the offline-update metadata and its snapshot"""

    # Helper function:
    def ensure(cond, message):
        if not cond:
            raise InvalidDataError("Error: " + message)

    log.debug("Validating offline-update metadata")

    now = datetime.now(timezone.utc)

    # Basic check of the snapshot metadata alone.
    snapshot_meta = offupd_snapshot_info["parsed"]["signed"]

    ensure(snapshot_meta["_type"] == "Offline-Snapshot",
           "_type in snapshot metadata does not equal 'Offline-Snapshot'")
    ensure(dateutil.parser.parse(snapshot_meta["expires"]) > now,
           "Offline snapshot metadata is already expired")

    # Basic check of the targets metadata alone.
    targets_meta = offupd_targets_info["parsed"]["signed"]

    ensure(targets_meta["_type"] == "Offline-Updates",
           "_type in targets metadata does not equal 'Offline-Updates'")

    ensure(dateutil.parser.parse(targets_meta["expires"]) > now,
           "Offline targets metadata is already expired")

    # Cross-checks:
    targets_file = os.path.basename(offupd_targets_info["file"])
    ensure(targets_file in snapshot_meta["meta"],
           f"{targets_file} is not described in the snapshot metadata")

    # The way the server determines the SHA is based on the canonical JSON
    # so we are skipping this check here (Aktualizr doesn't do it either):
    # ensure(snapshot_meta["meta"][targets_file]["hashes"]["sha256"] ==
    #        offupd_targets_info["sha256"],
    #        f"{targets_file} does not have the expected sha256")

    ensure(snapshot_meta["meta"][targets_file]["length"] ==
           offupd_targets_info["size"],
           f"{targets_file} does not have the expected size")

    ensure(snapshot_meta["meta"][targets_file]["version"] ==
           targets_meta["version"],
           f"{targets_file} does not have the expected version")

    # Maybe check signature (event though this is be done by the device) (TODO).
    log.info("Offline-update metadata passed basic validation")


def load_offupd_metadata(lockbox_name, source_dir):
    """Load the metadata for the specified lockbox name

    This function will load both the targets and the snapshot metadata for
    the specified offline-update lockbox.

    :param lockbox_name: Name of the lockbox (possibly with the extension .json)
    :param source_dir: Path to directory where metadata files are searched for.
    """

    # Special handling for the case where input is a local file:
    if lockbox_name.endswith(JSON_EXT):
        lockbox_name = os.path.basename(lockbox_name[:-len(JSON_EXT)])

    lockbox_file = os.path.join(source_dir, lockbox_name + JSON_EXT)

    # Load targets metadata into memory.
    log.info(f"Loading offline-update targets metadata from '{lockbox_file}'")
    offupd_targets_info = platform.load_metadata(lockbox_file)

    # Load snapshot metadata (search same directory as the targets metadata file is).
    offupd_snapshot_file = os.path.join(source_dir, OFFLINE_SNAPSHOT_FILE)
    log.info(f"Loading offline-update snapshot metadata from {offupd_snapshot_file}")
    offupd_snapshot_info = platform.load_metadata(offupd_snapshot_file)

    return offupd_targets_info, offupd_snapshot_info


# pylint: disable=too-many-locals
def fetch_offupdt_targets(
        offupdt_targets_info, imgrepo_targets_info,
        images_dir, docker_metadata_dir,
        ostree_url=None, repo_url=None, access_token=None,
        docker_platforms=None):
    """Fetch all targets referenced by the offline-update targets metadata

    :param offupdt_targets_info: Targets metadata of the offline-update.
    :param imgrepo_targets_info: Targets metadata of the image-repository.
    :param images_dir: Directory where images would be stored.
    :param docker_metadata_dir: Directory where to store metadata for Docker.
    :param ostree_url: Base URL of the OSTree repository.
    :param repo_url: Base URL of the TUF repository as it appears in the
                     credentials file.
    :param access_token: OAuth2 access token giving access to the TUF repos of
                         the user at the OTA server.
    :param docker_platforms: List of platforms for fetching Docker images by
                             default.
    """

    # offupdt_targets = offupdt_targets_info["parsed"]["signed"]["targets"]
    for offupdt_name, offupdt_meta in offupdt_targets_info["parsed"]["signed"]["targets"].items():
        offupdt_hash = offupdt_meta["hashes"]["sha256"]
        offupdt_len = offupdt_meta["length"]
        imgrepo_name, imgrepo_meta = platform.find_imgrepo_target(
            imgrepo_targets_info, offupdt_hash, offupdt_name, offupdt_len)

        if (imgrepo_name is None) or (imgrepo_meta is None):
            raise TorizonCoreBuilderError(
                f"Could not find target '{offupdt_name}' in image-repo metadata")

        tgtformat = imgrepo_meta["custom"]["targetFormat"]
        # TODO: Allow custom URIs ('uri' field?)
        # Handle each type of target.
        if tgtformat == "OSTREE":
            params = {
                "target": imgrepo_name,
                "sha256": imgrepo_meta["hashes"]["sha256"],
                "ostree_url": ostree_url,
                "images_dir": images_dir,
                "name": imgrepo_meta["custom"]["name"],
                "version": imgrepo_meta["custom"]["version"],
                "access_token": access_token
            }
            platform.fetch_ostree_target(**params)

        elif tgtformat == "BINARY":
            params = {
                "target": imgrepo_name,
                "repo_url": repo_url,
                "images_dir": images_dir,
                "name": imgrepo_meta["custom"]["name"],
                "version": imgrepo_meta["custom"]["version"],
                "access_token": access_token
            }
            # Currently we always check the sha and length of binary targets.
            params.update({
                "sha256": imgrepo_meta["hashes"]["sha256"],
                "length": imgrepo_meta["length"],
            })
            # Handle compose and basic binary files differently:
            if "docker-compose" in imgrepo_meta["custom"]["hardwareIds"]:
                params.update({
                    "req_platforms": docker_platforms,
                    "metadata_dir": docker_metadata_dir
                })
                platform.fetch_compose_target(**params)
            else:
                platform.fetch_binary_target(**params)

        else:
            assert False, \
                f"Do not know how to handle target of type {tgtformat}"
# pylint: enable=too-many-locals


# pylint: disable=too-many-locals
def platform_lockbox(
        lockbox_name, creds_file, output_dir,
        docker_logins=None, docker_platforms=None,
        force=False, validate=True, fetch_targets=True):
    """Main handler for the 'platform lockbox' subcommand

    :param lockbox_name: Name of the lockbox image as defined at the OTA server
                       or the name a JSON file with the snapshot data for the
                       lockbox image.
    :param creds_file: Name of the `credentials.zip` file.
    :param output_dir: Directory where the lockbox image will be created.
    :param docker_logins: A list-like object where one element is a pai
                          (username, password) to be used with the default
                          registry and the other items are 3-tuples
                          (registry, username, password) with authentication
                          information to be used with other registries.
    :param force: Whether to force the generation of the output directory.
    :param validate: Whether to validate the Uptane metadata.
    :param fetch_targets: Whether to fetch the actual targets.
    """

    # Create output directory or abort:
    if os.path.exists(output_dir):
        if force:
            log.debug(f"Removing existing output directory '{output_dir}'")
            shutil.rmtree(output_dir)
        else:
            raise InvalidStateError(
                f"Output directory '{output_dir}' already exists; please remove"
                " it or select another output directory.")

    os.makedirs(output_dir)

    # Build directory structure:
    images_dir = os.path.join(output_dir, IMAGES_DIR)
    director_dir = os.path.join(output_dir, DIRECTOR_DIR)
    imagerepo_dir = os.path.join(output_dir, IMAGEREPO_DIR)
    dockermeta_dir = os.path.join(output_dir, DOCKERMETA_DIR)

    os.makedirs(images_dir)
    os.makedirs(director_dir)
    os.makedirs(imagerepo_dir)
    os.makedirs(dockermeta_dir)

    try:
        # Configure Docker "operations" class.
        RegistryOperations.set_logins(docker_logins)

        # Load credentials file.
        server_creds = sotaops.ServerCredentials(creds_file)
        # log.debug(server_creds)

        # Get access token (this should be valid for hours).
        sota_token = sotaops.get_access_token(server_creds)

        # Fetch metadata from OTA server.
        log.info(l1_pref("Handle director-repository metadata"))
        platform.fetch_director_metadata(
            lockbox_name,
            server_creds.director_url, director_dir, access_token=sota_token)

        log.info(l1_pref("Handle image-repository metadata"))
        platform.fetch_imgrepo_metadata(
            server_creds.repo_url, imagerepo_dir, access_token=sota_token)

        log.info(l1_pref("Process metadata"))
        # Load and validate top-level metadata (offline targets and snapshot (director)):
        offupd_targets_info, offupd_snapshot_info = \
            load_offupd_metadata(lockbox_name, director_dir)
        if validate:
            validate_offupd_metadata(offupd_targets_info, offupd_snapshot_info)

        imgrepo_targets_info = platform.load_imgrepo_targets(imagerepo_dir)

        # Fetch all targets specified in offline-update targets metadata:
        if fetch_targets:
            log.info(l1_pref("Handle Uptane targets"))

            fetch_offupdt_targets(
                offupdt_targets_info=offupd_targets_info,
                imgrepo_targets_info=imgrepo_targets_info,
                ostree_url=server_creds.ostree_server,
                repo_url=server_creds.repo_url,
                images_dir=images_dir,
                access_token=sota_token,
                docker_metadata_dir=dockermeta_dir,
                docker_platforms=docker_platforms)
        else:
            log.info(l1_pref("Handle Uptane targets [skipped]"))

        common.set_output_ownership(output_dir, set_parents=True)

    except BaseException as exc:
        # Avoid leaving a damaged output around: we catch BaseException here
        # so that even keyboard interrupts are handled.
        if os.path.exists(output_dir):
            log.info(f"Removing output directory '{output_dir}' due to errors")
            shutil.rmtree(output_dir)
        raise exc
# pylint: enable=too-many-locals


def do_platform_lockbox(args):
    """Wrapper for 'platform lockbox' subcommand"""

    # Build list of logins:
    logins = []
    if args.main_login:
        logins.append(args.main_login)

    platform_lockbox(
        args.lockbox_name, args.credentials, args.output_directory,
        docker_logins=logins,
        docker_platforms=(args.platforms or DEFAULT_PLATFORMS),
        force=args.force,
        validate=args.validate,
        fetch_targets=args.fetch_targets)


def do_platform_push(args):
    """Wrapper for 'platform push' subcommand"""

    if args.canonicalize_only:
        # pylint: disable=singleton-comparison
        if args.canonicalize == False:
            raise TorizonCoreBuilderError(
                "Error: The '--no-canonicalize' and '--canonicalize-only' "
                "options cannot be used at the same time. Please, run "
                "'torizoncore-builder platform push --help' for more information.")
        lock_file, _ = platform.canonicalize_compose_file(args.ref, args.force)
        log.info(f"Not pushing '{os.path.basename(lock_file)}' to OTA server.")
        return

    if not args.credentials:
        raise TorizonCoreBuilderError("--credentials parameter is required.")

    storage_dir = os.path.abspath(args.storage_directory)
    credentials = os.path.abspath(args.credentials)

    if args.ref.endswith(".yml") or args.ref.endswith(".yaml"):
        if args.hardwareids and any(hwid != "docker-compose" for hwid in args.hardwareid):
            raise InvalidArgumentError("Error: --hardware is only valid when pushing "
                                       "OSTree reference. The hardware id for a "
                                       "docker-compose package can only be "
                                       "\"docker-compose\"")

        version = args.version or datetime.today().strftime("%Y-%m-%d")
        platform.push_compose(credentials, args.target, version, args.ref,
                              args.canonicalize, args.force)
    else:
        if args.ostree is not None:
            src_ostree_archive_dir = os.path.abspath(args.ostree)
        else:
            src_ostree_archive_dir = os.path.join(storage_dir, "ostree-archive")

        tuf_repo = os.path.join("/deploy", "tuf-repo")

        if not os.path.exists(storage_dir):
            raise PathNotExistError(f"{storage_dir} does not exist")

        platform.push_ref(src_ostree_archive_dir, tuf_repo, credentials,
                          args.ref, args.version, args.target, args.hardwareids,
                          args.verbose)


def add_common_push_arguments(subparser):
    """
    Add push arguments to a parser of a command
    """
    # TODO: IMPORTANT!! Remember to undo this once push command is completely removed
    subparser.add_argument(
        "--credentials", dest="credentials",
        help="Relative path to credentials.zip.")
    subparser.add_argument(
        "--repo", dest="ostree",
        help="OSTree repository to push from.", required=False)
    subparser.add_argument(
        "--hardwareid", dest="hardwareids", action="append",
        help=("Hardware ID to use when pushing an OSTree package (can be specified "
              "multiple times). Will allow this package to be compatible with "
              "devices of the same Hardware ID."),
        required=False, default=None)
    subparser.add_argument(
        "--package-name", dest="target",
        help=("Package name for docker-compose file (default: name of file being "
              "pushed to OTA) or OSTree reference (default: same as REF)."),
        required=False, default=None)
    subparser.add_argument(
        "--package-version", dest="version",
        help=("Package version for docker-compose file (default: current date "
              "following the 'yyyy-mm-dd' format) or OSTree reference "
              "(default: OSTree subject)."),
        required=False, default=None)
    subparser.add_argument(
        metavar="REF", dest="ref",
        help="OSTree reference or docker-compose file to push to Torizon OTA.")
    subparser.add_argument(
        "--canonicalize", dest="canonicalize", action=argparse.BooleanOptionalAction,
        help=("Generates a canonicalized version of the docker-compose file, changing "
              "its extension to '.lock.yml' or '.lock.yaml' and pushing it to Torizon "
              "OTA; The package name is the name of the generated file if no package "
              "name is provided."))
    subparser.add_argument(
        "--canonicalize-only", dest="canonicalize_only", action="store_true",
        help="Canonicalize the docker-compose.yml file but do not send it to OTA server.",
        required=False, default=False)
    subparser.add_argument(
        "--force", dest="force", action="store_true", default=False,
        help="Force removal of the canonicalized file if it already exists.")
    subparser.add_argument(
        "--verbose", dest="verbose",
        action="store_true",
        help="Show more output", required=False)


def init_parser(subparsers):
    """Initialize 'platform' subcommands command line interface."""

    parser = subparsers.add_parser(
        "platform",
        help=("Execute operations that interact with the Torizon Platform Services "
              "(app.torizon.io) or a compatible server"))
    subparsers = parser.add_subparsers(title='Commands', required=True, dest='cmd')

    # platform lockbox
    # TODO Include a link to the Documentation page describing offline-updates.
    subparser = subparsers.add_parser(
        "lockbox",
        help=("Generate a Lockbox for secure offline updates, "
              "in a format ready to copy to an SD Card or USB Stick"),
        epilog=("After the Lockbox is generated, the output directory "
                "should be copied (and possibly renamed) to the "
                "removable media used for the offline updates; the name "
                "of the directory in the media should be in accordance "
                "with the update client (aktualizr) configuration."))
    subparser.add_argument(
        dest="lockbox_name",
        metavar="LOCKBOX_NAME",
        help="Name of the Lockbox (as defined at the OTA server)")
    subparser.add_argument(
        "--credentials", dest="credentials",
        help="Relative path to credentials.zip.", required=True)
    subparser.add_argument(
        "--force", dest="force",
        default=False, action="store_true",
        help=("Force program output (remove output directory before "
              "generating the Lockbox image)."))
    subparser.add_argument(
        "--platform",
        action="append",
        metavar="PLATFORM",
        dest="platforms",
        help=("Define platform to select when not specified in the compose file "
              f"(can be specified multiple times; default: {', '.join(DEFAULT_PLATFORMS)})."))
    subparser.add_argument(
        "--login", nargs=2, dest="main_login",
        metavar=('USERNAME', 'PASSWORD'),
        help=("Request that the tool logs in to the default [Docker Hub] "
              "registry using specified USERNAME and PASSWORD."))
    subparser.add_argument(
        "--output-directory",
        help=("Relative path to the output directory (default: update/). If "
              "parent directories are passed such as in a/b/update/, they will "
              "be automatically created."),
        default="update/")
    # Hidden argument (disable basic metadata validation (expiry date, # of targets, etc.)):
    subparser.add_argument(
        "--no-validate",
        dest="validate",
        help=argparse.SUPPRESS,
        action="store_false", default=True)
    # Hidden argument (disable fetching of targets (that is, fetch only Uptane metadata)):
    subparser.add_argument(
        "--no-fetch-targets",
        dest="fetch_targets",
        help=argparse.SUPPRESS,
        action="store_false", default=True)

    subparser.set_defaults(func=do_platform_lockbox)

    # platform push
    subparser = subparsers.add_parser(
        "push",
        help="Push artifact to OTA server as a new update package.",
        epilog=("Note: for a docker-compose file to be suitable "
                "for use with offline-updates it must be in canonical "
                "form; this can be achieved by passing the "
                "'--canonicalize' switch to the program in which case "
                "the file will be translated into canonical "
                "form before being uploaded to the server."))

    add_common_push_arguments(subparser)

    subparser.set_defaults(func=do_platform_push)
