"""Signals for the task application"""
from django.db.models.signals import post_save

from email.utils import parseaddr
import logging

from muckrock.task.models import OrphanTask, BlacklistDomain

logger = logging.getLogger(__name__)

def domain_blacklist(sender, instance, **kwargs):
    """Blacklist certain domains - automatically reject tasks from them"""
    # pylint: disable=unused-argument

    if not kwargs['created']:
        # if this isn't being created for the first time, just return
        # to avoid an infinite loop from when we resolve the task
        return

    _, email = parseaddr(instance.communication.priv_from_who)

    if '@' not in email:
        return

    domain = email.split('@')[1]

    logger.info('Checking domain %s against blacklist', domain)

    if BlacklistDomain.objects.filter(domain=domain).exists():
        instance.resolve()

post_save.connect(domain_blacklist, sender=OrphanTask,
        dispatch_uid='muckrock.task.signals.domain_blacklist')