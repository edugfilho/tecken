# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import hashlib

from django.db import models
from django.utils.encoding import force_bytes


class MissingSymbol(models.Model):
    # Use this to quickly identify symbols when you need to look them up
    hash = models.CharField(max_length=32, unique=True)
    # Looking through 70,000 old symbol uploads, the longest
    # symbol was 39 char, debug ID 34 char, filename 44 char.
    # However, the missing symbols might be weird and wonderful so
    # allow for larger ones.
    symbol = models.CharField(max_length=150)
    debugid = models.CharField(max_length=150)
    filename = models.CharField(max_length=150)
    # These are optional because they only really apply when
    # symbol downloads are queried from stackwalker.
    code_file = models.CharField(max_length=150, null=True)
    code_id = models.CharField(max_length=150, null=True)
    # This is to keep track of every time we re-encounter this as missing.
    count = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True, db_index=True)

    def __repr__(self):
        return (
            f"<{self.__class__.__name__} id={self.id} "
            f"{self.symbol!r} / "
            f"{self.debugid!r} / "
            f"{self.filename!r}>"
        )

    @classmethod
    def make_md5_hash(cls, *strings):
        return hashlib.md5(
            force_bytes(":".join(x for x in strings if x is not None))
        ).hexdigest()
