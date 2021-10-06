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
from typing import Optional, List, TYPE_CHECKING, ClassVar

from asyncpg import Record
from attr import dataclass

from mautrix.types import UserID, RoomID
from mautrix.util.async_db import Database

fake_db = Database("") if TYPE_CHECKING else None


@dataclass
class User:
    db: ClassVar[Database] = fake_db

    mxid: UserID
    gcid: Optional[str]
    refresh_token: Optional[str]
    notice_room: Optional[RoomID]

    @classmethod
    def _from_row(cls, row: Optional[Record]) -> Optional['User']:
        if row is None:
            return None
        return cls(**row)

    @classmethod
    async def all_logged_in(cls) -> List['User']:
        rows = await cls.db.fetch('SELECT mxid, gcid, refresh_token, notice_room FROM "user" '
                                  "WHERE gcid IS NOT NULL AND refresh_token IS NOT NULL")
        return [cls._from_row(row) for row in rows]

    @classmethod
    async def get_by_gcid(cls, gcid: str) -> Optional['User']:
        q = 'SELECT mxid, gcid, refresh_token, notice_room FROM "user" WHERE gcid=$1'
        row = await cls.db.fetchrow(q, gcid)
        return cls._from_row(row)

    @classmethod
    async def get_by_mxid(cls, mxid: UserID) -> Optional['User']:
        q = 'SELECT mxid, gcid, refresh_token, notice_room FROM "user" WHERE mxid=$1'
        row = await cls.db.fetchrow(q, mxid)
        return cls._from_row(row)

    async def insert(self) -> None:
        q = 'INSERT INTO "user" (mxid, gcid, refresh_token, notice_room) VALUES ($1, $2, $3, $4)'
        await self.db.execute(q, self.mxid, self.gcid, self.refresh_token, self.notice_room)

    async def delete(self) -> None:
        await self.db.execute('DELETE FROM "user" WHERE mxid=$1', self.mxid)

    async def save(self) -> None:
        await self.db.execute('UPDATE "user" SET gcid=$2, refresh_token=$3, notice_room=$4 '
                              'WHERE mxid=$1',
                              self.mxid, self.gcid, self.refresh_token, self.notice_room)