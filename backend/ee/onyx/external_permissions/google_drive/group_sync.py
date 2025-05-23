from googleapiclient.errors import HttpError  # type: ignore

from ee.onyx.db.external_perm import ExternalUserGroup
from onyx.connectors.google_drive.connector import GoogleDriveConnector
from onyx.connectors.google_utils.google_utils import execute_paginated_retrieval
from onyx.connectors.google_utils.resources import AdminService
from onyx.connectors.google_utils.resources import get_admin_service
from onyx.connectors.google_utils.resources import get_drive_service
from onyx.db.models import ConnectorCredentialPair
from onyx.utils.logger import setup_logger

logger = setup_logger()


def _get_drive_members(
    google_drive_connector: GoogleDriveConnector,
    admin_service: AdminService,
) -> dict[str, tuple[set[str], set[str]]]:
    """
    This builds a map of drive ids to their members (group and user emails).
    E.g. {
        "drive_id_1": ({"group_email_1"}, {"user_email_1", "user_email_2"}),
        "drive_id_2": ({"group_email_3"}, {"user_email_3"}),
    }
    """

    # fetches shared drives only
    drive_ids = google_drive_connector.get_all_drive_ids()

    drive_id_to_members_map: dict[str, tuple[set[str], set[str]]] = {}
    drive_service = get_drive_service(
        google_drive_connector.creds,
        google_drive_connector.primary_admin_email,
    )

    admin_user_info = (
        admin_service.users()
        .get(userKey=google_drive_connector.primary_admin_email)
        .execute()
    )
    is_admin = admin_user_info.get("isAdmin", False) or admin_user_info.get(
        "isDelegatedAdmin", False
    )

    for drive_id in drive_ids:
        group_emails: set[str] = set()
        user_emails: set[str] = set()

        try:
            for permission in execute_paginated_retrieval(
                drive_service.permissions().list,
                list_key="permissions",
                fileId=drive_id,
                fields="permissions(emailAddress, type),nextPageToken",
                supportsAllDrives=True,
                # can only set `useDomainAdminAccess` to true if the user
                # is an admin
                useDomainAdminAccess=is_admin,
            ):
                if permission["type"] == "group":
                    group_emails.add(permission["emailAddress"])
                elif permission["type"] == "user":
                    user_emails.add(permission["emailAddress"])
        except HttpError as e:
            if e.status_code == 404:
                logger.warning(
                    f"Error getting permissions for drive id {drive_id}. "
                    f"User '{google_drive_connector.primary_admin_email}' likely "
                    f"does not have access to this drive. Exception: {e}"
                )
            else:
                raise e

        drive_id_to_members_map[drive_id] = (group_emails, user_emails)
    return drive_id_to_members_map


def _get_all_groups(
    admin_service: AdminService,
    google_domain: str,
) -> set[str]:
    """
    This gets all the group emails.
    """
    group_emails: set[str] = set()
    for group in execute_paginated_retrieval(
        admin_service.groups().list,
        list_key="groups",
        domain=google_domain,
        fields="groups(email),nextPageToken",
    ):
        group_emails.add(group["email"])
    return group_emails


def _map_group_email_to_member_emails(
    admin_service: AdminService,
    group_emails: set[str],
) -> dict[str, set[str]]:
    """
    This maps group emails to their member emails.
    """
    group_to_member_map: dict[str, set[str]] = {}
    for group_email in group_emails:
        group_member_emails: set[str] = set()
        for member in execute_paginated_retrieval(
            admin_service.members().list,
            list_key="members",
            groupKey=group_email,
            fields="members(email),nextPageToken",
        ):
            group_member_emails.add(member["email"])

        group_to_member_map[group_email] = group_member_emails
    return group_to_member_map


def _build_onyx_groups(
    drive_id_to_members_map: dict[str, tuple[set[str], set[str]]],
    group_email_to_member_emails_map: dict[str, set[str]],
) -> list[ExternalUserGroup]:
    onyx_groups: list[ExternalUserGroup] = []

    # Convert all drive member definitions to onyx groups
    # This is because having drive level access means you have
    # irrevocable access to all the files in the drive.
    for drive_id, (group_emails, user_emails) in drive_id_to_members_map.items():
        all_member_emails: set[str] = user_emails
        for group_email in group_emails:
            if group_email not in group_email_to_member_emails_map:
                logger.warning(
                    f"Group email {group_email} not found in group_email_to_member_emails_map"
                )
                continue
            all_member_emails.update(group_email_to_member_emails_map[group_email])
        onyx_groups.append(
            ExternalUserGroup(
                id=drive_id,
                user_emails=list(all_member_emails),
            )
        )

    # Convert all group member definitions to onyx groups
    for group_email, member_emails in group_email_to_member_emails_map.items():
        onyx_groups.append(
            ExternalUserGroup(
                id=group_email,
                user_emails=list(member_emails),
            )
        )

    return onyx_groups


def gdrive_group_sync(
    tenant_id: str,
    cc_pair: ConnectorCredentialPair,
) -> list[ExternalUserGroup]:
    # Initialize connector and build credential/service objects
    google_drive_connector = GoogleDriveConnector(
        **cc_pair.connector.connector_specific_config
    )
    google_drive_connector.load_credentials(cc_pair.credential.credential_json)
    admin_service = get_admin_service(
        google_drive_connector.creds, google_drive_connector.primary_admin_email
    )

    # Get all drive members
    drive_id_to_members_map = _get_drive_members(google_drive_connector, admin_service)

    # Get all group emails
    all_group_emails = _get_all_groups(
        admin_service, google_drive_connector.google_domain
    )

    # Map group emails to their members
    group_email_to_member_emails_map = _map_group_email_to_member_emails(
        admin_service, all_group_emails
    )

    # Convert the maps to onyx groups
    onyx_groups = _build_onyx_groups(
        drive_id_to_members_map=drive_id_to_members_map,
        group_email_to_member_emails_map=group_email_to_member_emails_map,
    )

    return onyx_groups
