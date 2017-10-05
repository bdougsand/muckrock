# -*- coding: utf-8 -*-
# Generated by Django 1.11.4 on 2017-10-04 13:53
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('agency', '0009_agency_manual_stale'),
    ]

    operations = [
        migrations.CreateModel(
            name='AgencyAddress',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('request_type', models.CharField(choices=[(b'primary', b'Primary'), (b'appeal', b'Appeal'), (b'none', b'None')], default=b'none', max_length=7)),
            ],
        ),
        migrations.CreateModel(
            name='AgencyEmail',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('request_type', models.CharField(choices=[(b'primary', b'Primary'), (b'appeal', b'Appeal'), (b'none', b'None')], default=b'none', max_length=7)),
                ('email_type', models.CharField(choices=[(b'to', b'To'), (b'cc', b'CC'), (b'none', b'None')], default=b'none', max_length=4)),
            ],
        ),
        migrations.CreateModel(
            name='AgencyPhone',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('request_type', models.CharField(choices=[(b'primary', b'Primary'), (b'appeal', b'Appeal'), (b'none', b'None')], default=b'none', max_length=7)),
                ('agency', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='agency.Agency')),
            ],
        ),
    ]