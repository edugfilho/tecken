# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# Generated by Django 2.0.7 on 2018-08-22 17:33

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("upload", "0017_auto_20180112_1907"),
    ]

    operations = [
        migrations.CreateModel(
            name="UploadsCreated",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("date", models.DateField(db_index=True, unique=True)),
                ("count", models.PositiveIntegerField()),
                ("files", models.PositiveIntegerField()),
                ("skipped", models.PositiveIntegerField()),
                ("ignored", models.PositiveIntegerField()),
                ("size", models.PositiveIntegerField()),
                ("size_avg", models.PositiveIntegerField()),
                ("modified_at", models.DateTimeField(auto_now=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]
