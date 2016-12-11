"""
Miscellanous utilities
"""

import actstream
import datetime
import random
import string
import stripe
from queued_storage.backends import QueuedStorage

from django.conf import settings
from django.contrib.auth.models import User, Group
from django.core.cache import cache
from django.template import Context
from django.template.loader_tags import BlockNode, ExtendsNode
from django.utils.module_loading import import_string

#From http://stackoverflow.com/questions/2687173/django-how-can-i-get-a-block-from-a-template

class BlockNotFound(Exception):
    """Block not found exception"""
    pass


def get_node(template, context=Context(), name='subject'):
    """Render one block from a template"""
    for node in template:
        if isinstance(node, BlockNode) and node.name == name:
            return node.render(context)
        elif isinstance(node, ExtendsNode):
            return get_node(node.nodelist, context, name)
    raise BlockNotFound("Node '%s' could not be found in template." % name)


def new_action(actor, verb, action_object=None, target=None, public=True, description=None):
    """Wrapper to send a new action and return the generated Action object."""
    # pylint: disable=too-many-arguments
    action_signal = actstream.action.send(
        actor,
        verb=verb,
        action_object=action_object,
        target=target,
        public=public,
        description=description)
    # action_signal = ((action_handler, Action))
    return action_signal[0][1]


def generate_status_action(foia):
    """Generate activity stream action for agency response and return it."""
    if not foia.agency:
        return
    verbs = {
        'rejected': 'rejected',
        'done': 'completed',
        'partial': 'partially completed',
        'processed': 'acknowledged',
        'no_docs': 'has no responsive documents',
        'fix': 'requires fix',
        'payment': 'requires payment',
    }
    verb = verbs.get(foia.status, 'is processing')
    return new_action(foia.agency, verb, target=foia)


def notify(users, action):
    """Notify a set of users about an action and return the list of notifications."""
    from muckrock.accounts.models import Notification
    notifications = []
    if isinstance(users, Group):
        # If users is a group, get the queryset of users
        users = users.user_set.all()
    elif isinstance(users, User):
        # If users is a single user, make it into a list
        users = [users]
    if action is None:
        # If no action is provided, don't generate any notifications
        return notifications
    for user in users:
        notification = Notification.objects.create(user=user, action=action)
        notifications.append(notification)
    return notifications


def generate_key(size=6, chars=string.ascii_uppercase + string.digits):
    """Generates a random alphanumeric key"""
    return ''.join(random.SystemRandom().choice(chars) for _ in range(size))


def get_stripe_token(card_number='4242424242424242'):
    """
    Helper function for creating a dummy Stripe token.
    Normally, the token would be generated by Stripe Checkout on the front end.
    Allows a different card number to be passed in to simulate different error cases.
    """
    card = {
        "number": card_number,
        "exp_month": datetime.date.today().month,
        "exp_year": datetime.date.today().year,
        "cvc": '123'
    }
    token = stripe.Token.create(card=card)
    # all we need for testing stripe calls is the token id
    return token.id


def cache_get_or_set(key, update, timeout):
    """Get the value from the cache if present, otherwise update it"""
    value = cache.get(key)
    if value is None:
        value = update()
        cache.set(key, value, timeout)
    return value


def get_image_storage():
    """Return the storage class to use for images we want optimized"""
    if settings.USE_QUEUED_STORAGE:
        return QueuedStorage(
                'storages.backends.s3boto.S3BotoStorage',
                'image_diet.storage.DietStorage',
                remote_options={'file_overwrite': True},
                task='queued_storage.tasks.Transfer')
    else:
        return import_string(settings.DEFAULT_FILE_STORAGE)()
