# -*- coding: utf-8 -*-
# Generated by Django 1.9.9 on 2017-02-20 12:24
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('foiamachine', '0008_auto_20170212_1728'),
    ]

    operations = [
        migrations.AlterField(
            model_name='foiamachinerequest',
            name='jurisdiction',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='jurisdiction.Jurisdiction'),
        ),
    ]