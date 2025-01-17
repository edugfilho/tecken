# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# FIXME(willkg): remove after we remove celery infra
from .celery import app as celery_app

default_app_config = "tecken.apps.TeckenAppConfig"


# This will make sure the app is always imported when
# Django starts so that shared_task will use this app.
__all__ = ["celery_app", "default_app_config"]
