# mautrix-googlechat - A Matrix-Google Chat puppeting bridge
# Copyright (C) 2021 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from mautrix.client import Client
from mautrix.bridge import custom_puppet as cpu
from mautrix.bridge.commands import HelpSection, command_handler

from hangups import googlechat_pb2 as googlechat

from .. import puppet as pu
from .typehint import CommandEvent

SECTION_AUTH = HelpSection("Authentication", 10, "")


@command_handler(needs_auth=False, management_only=True,
                 help_section=SECTION_AUTH, help_text="Log in to Hangouts")
async def login(evt: CommandEvent) -> None:
    token = evt.bridge.auth_server.make_token(evt.sender.mxid)
    public_prefix = evt.config["bridge.web.auth.public"]
    url = f"{public_prefix}#{token}"
    await evt.reply(f"Please visit the [login portal]({url}) to log in.")


@command_handler(needs_auth=True, management_only=True, help_section=SECTION_AUTH)
async def logout(evt: CommandEvent) -> None:
    puppet = pu.Puppet.get_by_gid(evt.sender.gid)
    await evt.sender.logout()
    if puppet and puppet.is_real_user:
        await puppet.switch_mxid(None, None)


@command_handler(needs_auth=True, management_only=True, help_section=SECTION_AUTH)
async def ping(evt: CommandEvent) -> None:
    try:
        info = await evt.sender.client.get_self_user_status(googlechat.GetSelfUserStatusRequest(
            request_header=evt.sender.client.get_gc_request_header()
        ))
        get_members_response = await evt.sender.client.get_members(
            googlechat.GetMembersRequest(
                request_header=evt.sender.client.get_gc_request_header(),
                member_ids=[googlechat.MemberId(
                    user_id=googlechat.UserId(id=info.user_status.user_id.id)
                )]
            )
        )
        self_info = get_members_response.members[0].user
    except Exception as e:
        evt.log.exception("Failed to get user info", exc_info=True)
        await evt.reply(f"Failed to get user info: {e}")
        return
    name = self_info.name
    email = f" &lt;{self_info.email}&gt;" if self_info.email else ""
    id = self_info.user_id.id
    await evt.reply(f"You're logged in as {name}{email} ({id})", allow_html=False)


@command_handler(needs_auth=False, management_only=True, help_section=SECTION_AUTH,
                 help_text="Mark this room as your bridge notice room")
async def set_notice_room(evt: CommandEvent) -> None:
    evt.sender.notice_room = evt.room_id
    evt.sender.save()
    await evt.reply("This room has been marked as your bridge notice room")
