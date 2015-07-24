"""
Nodes and tags for rendering crowdfunds into templates
"""

from django import template
from django.core.urlresolvers import reverse
from django.shortcuts import get_object_or_404

from muckrock.crowdfund.models import CrowdfundProject, CrowdfundRequest
from muckrock.crowdfund.forms import CrowdfundRequestPaymentForm, CrowdfundProjectPaymentForm
from muckrock.settings import STRIPE_PUB_KEY

register = template.Library()

def list_to_english_string(the_list):
    """A utility function to convert a list into an English string"""
    # convert list items to strings and remove empty strings
    str_list = [str(each_item) for each_item in the_list if str(each_item)]
    num_str = len(str_list)
    ret_str = ''
    # base case is that the list is empty
    if num_str == 0:
        return ret_str
    # construct an English list based on the number of items
    last_str = str_list[num_str - 1]
    if num_str == 1:
        ret_str = last_str
    elif num_str == 2:
        ret_str = str_list[0] + ' and ' + last_str
    else:
        sans_last_str = str_list[:num_str - 1]
        ret_str = (', ').join(sans_last_str) + ', and ' + last_str
    return ret_str

def crowdfund_form(crowdfund, form):
    """Returns a form initialized with crowdfund data"""
    initial_data = {'crowdfund': crowdfund.pk}
    default_amount = 25
    if crowdfund.amount_remaining() < default_amount:
        initial_data['amount'] = int(crowdfund.amount_remaining()) * 100
    else:
        initial_data['amount'] = default_amount * 100
    return form(initial=initial_data)

def crowdfund_user(context):
    """Returns a tuple of user information"""
    logged_in = context['user'].is_authenticated()
    user_email = context['user'].email if logged_in else ''
    return (logged_in, user_email)

def contributor_summary(crowdfund):
    """Returns a summary of the contributors to the project"""
    anonymous = 0
    contributor_names = []
    unnamed_string = ''
    for contributor in crowdfund.contributors():
        if contributor.is_anonymous():
            anonymous += 1
        else:
            contributor_names.append(contributor.get_full_name())
    # limit named contributors to `named_limit`
    named_limit = 4
    num_unnamed = len(contributor_names) - named_limit
    if num_unnamed < 0:
        num_unnamed = 0
    if anonymous > 0 or num_unnamed > 0:
        unnamed_string = str(num_unnamed + anonymous)
    # if named and unnamed together, use 'other(s)'
    if unnamed_string and len(contributor_names) > 0:
        unnamed_string += ' other'
        if (anonymous + num_unnamed) > 1:
            unnamed_string += 's'
    # if only unnamed, use 'person/people'
    else:
        if (anonymous + num_unnamed) > 1:
            unnamed_string += ' people'
        else:
            unnamed_string += ' person'
    if len(crowdfund.contributors()) > 0:
        summary = ('Supported by '
                   + list_to_english_string(contributor_names[:named_limit] + [unnamed_string])
                   + '.')
    else:
        summary = 'No contributors yet. Be the first!'
    return summary

def generate_crowdfund_context(the_crowdfund, the_url_name, the_form, the_context):
    """Generates context in a way that's agnostic towards the object being crowdfunded."""
    endpoint = reverse(the_url_name, kwargs={'pk': the_crowdfund.pk})
    payment_form = crowdfund_form(the_crowdfund, the_form)
    logged_in, user_email = crowdfund_user(the_context)
    contrib_sum = contributor_summary(the_crowdfund)
    return {
        'crowdfund': the_crowdfund,
        'contributor_summary': contrib_sum,
        'endpoint': endpoint,
        'logged_in': logged_in,
        'user_email': user_email,
        'payment_form': payment_form,
        'stripe_pk': STRIPE_PUB_KEY
    }

@register.inclusion_tag('crowdfund/widget.html', takes_context=True)
def crowdfund_request(context, crowdfund_pk):
    """Template tag to insert a crowdfunding panel"""
    the_crowdfund = get_object_or_404(CrowdfundRequest, pk=crowdfund_pk)
    return generate_crowdfund_context(
        the_crowdfund,
        'crowdfund-request',
        CrowdfundRequestPaymentForm,
        context
    )

@register.inclusion_tag('crowdfund/widget.html', takes_context=True)
def crowdfund_project(context, crowdfund_pk):
    """Template tag to insert a crowdfunding widget"""
    the_crowdfund = get_object_or_404(CrowdfundProject, pk=crowdfund_pk)
    return generate_crowdfund_context(
        the_crowdfund,
        'crowdfund-project',
        CrowdfundProjectPaymentForm,
        context
    )