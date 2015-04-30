
"""
Tasks for the account application
"""

from celery.schedules import crontab
from celery.task import periodic_task
from django.contrib.auth.models import User
from django.db.models import Sum

import gdata.analytics.service
import logging
from datetime import date, timedelta

from muckrock.accounts.models import Profile, Statistics
from muckrock.agency.models import Agency
from muckrock.foia.models import FOIARequest, FOIAFile, FOIACommunication
from muckrock.news.models import Article
from muckrock.settings import GA_USERNAME, GA_PASSWORD, GA_ID
from muckrock.task.models import Task, OrphanTask, SnailMailTask, RejectedEmailTask, \
                                 StaleAgencyTask, FlaggedTask, NewAgencyTask, ResponseTask

logger = logging.getLogger(__name__)

@periodic_task(run_every=crontab(hour=0, minute=30), name='muckrock.accounts.tasks.store_statstics')
def store_statstics():
    """Store the daily statistics"""

    yesterday = date.today() - timedelta(1)

    client = gdata.analytics.service.AnalyticsDataService()
    client.ssl = True
    client.ClientLogin(GA_USERNAME, GA_PASSWORD)
    data = client.GetData(ids=GA_ID, metrics='ga:pageviews', start_date=yesterday.isoformat(),
                          end_date=yesterday.isoformat())
    total_page_views = data.entry[0].pageviews.value

    stats = Statistics.objects.create(
        date=yesterday,
        total_requests=FOIARequest.objects.count(),
        total_requests_success=FOIARequest.objects.filter(status='done').count(),
        total_requests_denied=FOIARequest.objects.filter(status='rejected').count(),
        total_requests_draft=FOIARequest.objects.filter(status='started').count(),
        total_requests_submitted=FOIARequest.objects.filter(status='submitted').count(),
        total_requests_awaiting_ack=FOIARequest.objects.filter(status='ack').count(),
        total_requests_awaiting_response=FOIARequest.objects.filter(status='processed').count(),
        total_requests_awaiting_appeal=FOIARequest.objects.filter(status='appealing').count(),
        total_requests_fix_required=FOIARequest.objects.filter(status='fix').count(),
        total_requests_payment_required=FOIARequest.objects.filter(status='payment').count(),
        total_requests_no_docs=FOIARequest.objects.filter(status='no_docs').count(),
        total_requests_partial=FOIARequest.objects.filter(status='partial').count(),
        total_requests_abandoned=FOIARequest.objects.filter(status='abandoned').count(),
        total_pages=FOIAFile.objects.aggregate(Sum('pages'))['pages__sum'],
        total_users=User.objects.count(),
        total_agencies=Agency.objects.count(),
        total_fees=FOIARequest.objects.aggregate(Sum('price'))['price__sum'],
        pro_users=Profile.objects.filter(acct_type='pro').count(),
        pro_user_names=';'.join(p.user.username for p in Profile.objects.filter(acct_type='pro')),
        total_page_views=total_page_views,
        daily_requests_pro=FOIARequest.objects.filter(
            user__profile__acct_type='pro',
            date_submitted=yesterday
        ).count(),
        daily_requests_community=FOIARequest.objects.filter(
            user__profile__acct_type='community',
            date_submitted=yesterday
        ).count(),
        daily_requests_beta=FOIARequest.objects.filter(
            user__profile__acct_type='beta',
            date_submitted=yesterday
        ).count(),
        daily_articles=Article.objects.filter(pub_date__gte=yesterday,
                                              pub_date__lte=date.today()).count(),
        orphaned_communications=FOIACommunication.objects.filter(foia=None).count(),
        stale_agencies=Agency.objects.filter(stale=True).count(),
        unapproved_agencies=Agency.objects.filter(approved=False).count(),
        total_tasks=Task.objects.count(),
        total_unresolved_tasks=Task.objects.filter(resolved=False).count(),
        total_generic_tasks=GenericTask.objects.count(),
        total_unresolved_generic_tasks=GenericTask.objects.filter(resolved=False).count(),
        total_orphan_tasks=OrphanTask.objects.count(),
        total_unresolved_orphan_tasks=OrphanTask.objects.filter(resolved=False).count(),
        total_snailmail_tasks=SnailMailTask.objects.count(),
        total_unresolved_snailmail_tasks=SnailMailTask.objects.filter(resolved=False).count(),
        total_rejected_tasks=RejectedTask.objects.count(),
        total_unresolved_rejected_tasks=RejectedTask.objects.filter(resolved=False).count(),
        total_staleagency_tasks=StaleAgencyTask.objects.count(),
        total_unresolved_staleagency_tasks=StaleAgencyTask.objects.filter(resolved=False).count(),
        total_flagged_tasks=FlaggedTask.objects.count(),
        total_unresolved_flagged_tasks=FlaggedTask.objects.filter(resolved=False).count(),
        total_newagency_tasks=NewAgencyTask.objects.count(),
        total_unresolved_newagency_tasks=NewAgencyTask.objects.filter(resolved=False).count(),
        total_response_tasks=ResponseTask.objects.count(),
        total_unresolved_response_tasks=ResponseTask.objects.filter(resolved=False).count(),
        )
    # stats needs to be saved before many to many relationships can be set
    stats.users_today = User.objects.filter(last_login__year=yesterday.year,
                                            last_login__month=yesterday.month,
                                            last_login__day=yesterday.day)
    stats.save()

def _notices(email_pref):
    """Send out notices"""
    for profile in Profile.objects.filter(email_pref=email_pref, notifications__isnull=False)\
                          .distinct():
        profile.send_notifications()

@periodic_task(run_every=crontab(hour=10, minute=0), name='muckrock.accounts.tasks.daily_notices')
def daily_notices():
    """Send out daily notices"""
    _notices('daily')

@periodic_task(run_every=crontab(day_of_week='mon', hour=10, minute=0),
               name='muckrock.accounts.tasks.weekly')
def weekly_notices():
    """Send out weekly notices"""
    _notices('weekly')
