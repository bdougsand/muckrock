"""
FOIA views for actions
"""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect
from django.shortcuts import render_to_response, get_object_or_404, redirect
from django.template import RequestContext

from collections import namedtuple
from datetime import datetime, timedelta
import logging
import stripe
import sys

from muckrock.crowdfund.forms import CrowdfundRequestForm
from muckrock.foia.forms import \
    FOIADeleteForm, \
    FOIAAdminFixForm, \
    FOIANoteForm, \
    FOIAEmbargoForm, \
    FOIAFileFormSet
from muckrock.foia.models import FOIARequest, FOIAFile, END_STATUS
from muckrock.foia.views.comms import save_foia_comm
from muckrock.jurisdiction.models import Jurisdiction
from muckrock.settings import STRIPE_SECRET_KEY
from muckrock.task.models import PaymentTask

logger = logging.getLogger(__name__)
stripe.api_key = STRIPE_SECRET_KEY

RequestAction = namedtuple(
    'RequestAction',
    'form_actions msg tests form_class return_url heading value must_own template extra_context'
)

action_template = 'forms/base_form.html'

# Helper Functions

def _get_foia(jurisdiction, jidx, slug, idx):
    """Returns a foia object"""
    jmodel = get_object_or_404(Jurisdiction, slug=jurisdiction, pk=jidx)
    foia = get_object_or_404(FOIARequest, jurisdiction=jmodel, slug=slug, id=idx)
    return foia

def _foia_action(request, foia, action):
    """Generic helper for FOIA actions"""
    form_class = action.form_class(request, foia)
    # Check that the request belongs to the user
    if action.must_own and not foia.editable_by(request.user) and not request.user.is_staff:
        msg = 'You may only %s your own requests.' % action.msg
        messages.error(request, msg)
        return redirect(foia)
    # Check that the action is valid
    for test, msg in action.tests:
        if not test(foia):
            messages.error(request, msg)
            return redirect(foia)

    if request.method == 'POST':
        form = form_class(request.POST)
        if form.is_valid():
            action.form_actions(request, foia, form)
            return HttpResponseRedirect(action.return_url(request, foia))
    else:
        if isinstance(form_class, type) and issubclass(form_class, forms.ModelForm):
            form = form_class(instance=foia)
        else:
            form = form_class()

    context = action.extra_context(foia)
    args = {
        'form': form,
        'foia': foia,
        'heading': action.heading,
        'action': action.value
    }
    context.update(args)
    return render_to_response(
        action.template,
        context,
        context_instance=RequestContext(request)
    )

# User Actions

@login_required
def note(request, jurisdiction, jidx, slug, idx):
    """Add a note to a request"""
    def form_actions(_, foia, form):
        """Helper class, passed to generic function"""
        foia_note = form.save(commit=False)
        foia_note.foia = foia
        foia_note.date = datetime.now()
        foia_note.save()
    foia = _get_foia(jurisdiction, jidx, slug, idx)
    action = RequestAction(
        form_actions=form_actions,
        msg='add notes',
        tests=[],
        form_class=lambda r, f: FOIANoteForm,
        return_url=lambda r, f: f.get_absolute_url() + '#tabs-notes',
        heading='Add Note',
        value='Add',
        must_own=True,
        template=action_template,
        extra_context=lambda f: {}
    )
    return _foia_action(request, foia, action)

@login_required
def delete(request, jurisdiction, jidx, slug, idx):
    """Delete a non-submitted FOIA Request"""
    def form_actions(request, foia, _):
        """Helper class, passed to generic function"""
        foia.delete()
        messages.success(request, 'The draft has been deleted.')
    foia = _get_foia(jurisdiction, jidx, slug, idx)
    action = RequestAction(
        form_actions=form_actions,
        msg='delete',
        tests=[(
            lambda f: f.is_deletable(),
            'You can only delete drafts.'
        )],
        form_class=lambda r, f: FOIADeleteForm,
        return_url=lambda r, f: reverse('foia-mylist'),
        heading='Delete FOI Request',
        value='Delete',
        must_own=True,
        template=action_template,
        extra_context=lambda f: {}
    )
    return _foia_action(request, foia, action)

@login_required
def embargo(request, jurisdiction, jidx, slug, idx):
    """Change the embargo on a request"""

    def fine_tune_embargo(request, foia):
        """Adds an expiration date or makes permanent if necessary."""
        permanent = request.POST.get('permanent_embargo')
        expiration = request.POST.get('date_embargo')
        form = FOIAEmbargoForm({
            'permanent_embargo': request.POST.get('permanent_embargo'),
            'date_embargo': request.POST.get('date_embargo')
        })
        if form.is_valid():
            permanent = form.cleaned_data['permanent_embargo']
            expiration = form.cleaned_data['date_embargo']
            if request.user.profile.can_embargo_permanently():
                foia.permanent_embargo = permanent
            if expiration and foia.status in END_STATUS:
                foia.date_embargo = expiration
            foia.save()
        return

    def create_embargo(request, foia):
        """Apply an embargo to the FOIA"""
        if request.user.profile.can_embargo():
            foia.embargo = True
            foia.save()
            logger.info('%s embargoed %s', request.user, foia)
            fine_tune_embargo(request, foia)
        else:
            logger.error('%s was forbidden from embargoing %s', request.user, foia)
            messages.error(request, 'You cannot embargo requests.')
        return

    def update_embargo(request, foia):
        """Update an embargo to the FOIA"""
        if request.user.profile.can_embargo():
            fine_tune_embargo(request, foia)
        else:
            logger.error('%s was forbidden from updating the embargo on %s', request.user, foia)
            messages.error(request, 'You cannot update this embargo.')
        return

    def delete_embargo(request, foia):
        """Remove an embargo from the FOIA"""
        foia.embargo = False
        foia.save()
        logger.info('%s unembargoed %s', request.user, foia)
        return

    foia = _get_foia(jurisdiction, jidx, slug, idx)
    if request.method == 'POST' and foia.editable_by(request.user):
        embargo_action = request.POST.get('embargo')
        if embargo_action == 'create':
            create_embargo(request, foia)
        elif embargo_action == 'update':
            update_embargo(request, foia)
        elif embargo_action == 'delete':
            delete_embargo(request, foia)
    return redirect(foia)

@login_required
def pay_request(request, jurisdiction, jidx, slug, idx):
    """Pay us through CC for the payment on a request"""
    foia = _get_foia(jurisdiction, jidx, slug, idx)
    token = request.POST.get('stripe_token', False)
    email = request.POST.get('stripe_email', False)
    amount = request.POST.get('amount', False)
    if token and email and amount:
        try:
            request.user.profile.pay(
                token,
                amount,
                'Charge for request: %s %s' % (foia.title, foia.pk)
            )
        except stripe.CardError as exc:
            messages.error(request, 'Payment error: %s' % exc)
            logger.error('Payment error: %s', exc, exc_info=sys.exc_info())
            return redirect(foia)
        msg = 'Your payment was successful. We will get this to the agency right away.'
        messages.success(request, msg)
        logger.info(
            '%s has paid %0.2f for request %s',
            request.user.username,
            int(amount)/100,
            foia.title
        )
        foia.status = 'processed'
        foia.save()
        PaymentTask.objects.create(
            user=request.user,
            amount=int(amount)/100.0,
            foia=foia)
    return redirect(foia)

@login_required
def follow(request, jurisdiction, jidx, slug, idx):
    """Follow or unfollow a request"""
    foia = _get_foia(jurisdiction, jidx, slug, idx)
    if foia.user != request.user:
        followers = foia.followed_by
        if followers.filter(user=request.user): # If following, unfollow
            followers.remove(request.user.profile)
            msg = 'You are no longer following %s' % foia.title
        else: # If not following, follow
            followers.add(request.user.profile)
            msg = ('You are now following %s. '
                   'We will notify you when it is updated.') % foia.title
        messages.success(request, msg)
    else:
        messages.error(request, 'You may not follow your own request.')
    return redirect(foia)

@login_required
def toggle_autofollowups(request, jurisdiction, jidx, slug, idx):
    """Toggle autofollowups"""
    foia = _get_foia(jurisdiction, jidx, slug, idx)

    if foia.editable_by(request.user):
        foia.disable_autofollowups = not foia.disable_autofollowups
        foia.save()
        action = 'disabled' if foia.disable_autofollowups else 'enabled'
        msg = 'Autofollowups have been %s' % action
        messages.success(request, msg)
    else:
        msg = 'You must own the request to toggle auto-followups.'
        messages.error(request, msg)
    return redirect(foia)

# Staff Actions
@user_passes_test(lambda u: u.is_staff)
def admin_fix(request, jurisdiction, jidx, slug, idx):
    """Send an email from the requests auto email address"""
    foia = _get_foia(jurisdiction, jidx, slug, idx)

    if request.method == 'POST':
        form = FOIAAdminFixForm(request.POST)
        formset = FOIAFileFormSet(request.POST, request.FILES)
        if form.is_valid() and formset.is_valid():
            if form.cleaned_data['email']:
                foia.email = form.cleaned_data['email']
            if form.cleaned_data['other_emails']:
                foia.other_emails = form.cleaned_data['other_emails']
            if form.cleaned_data['from_email']:
                from_who = form.cleaned_data['from_email']
            else:
                from_who = foia.user.get_full_name()
            save_foia_comm(
                foia,
                from_who,
                form.cleaned_data['comm'],
                formset,
                snail=form.cleaned_data['snail_mail']
            )
            messages.success(request, 'Admin Fix submitted')
            return redirect(foia)
    else:
        form = FOIAAdminFixForm(instance=foia)
        formset = FOIAFileFormSet(queryset=FOIAFile.objects.none())
    context = {
        'form': form,
        'foia': foia,
        'heading': 'Email from Request Address',
        'formset': formset,
        'action': 'Submit'
    }
    return render_to_response(
        'forms/foia/admin_fix.html',
        context,
        context_instance=RequestContext(request)
    )

@login_required
def crowdfund_request(request, idx, **kwargs):
    """Crowdfund a request"""
    # pylint: disable=unused-argument
    foia = FOIARequest.objects.get(pk=idx)
    owner_or_staff = request.user == foia.user or request.user.is_staff

    # check for unauthorized access
    if not owner_or_staff:
        messages.error(request, 'You may only crowdfund your own requests.')
        return redirect(foia)
    if foia.has_crowdfund():
        messages.error(request, 'You may only run one crowdfund per request.')
        return redirect(foia)
    if foia.status != 'payment':
        messages.error(request, 'You may only crowfund when payment is required.')
        return redirect(foia)

    if request.method == 'POST':
        # save crowdfund object
        form = CrowdfundRequestForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Your crowdfund has started, spread the word!')
            return redirect(foia)

    elif request.method == 'GET':
        # create crowdfund form
        default_crowdfund_duration = 30
        date_due = datetime.now() + timedelta(default_crowdfund_duration)
        initial = {
            'name': u'Crowdfund Request: %s' % unicode(foia),
            'description': 'Help cover the request fees needed to free these docs!',
            'payment_required': foia.price,
            'date_due': date_due,
            'foia': foia
        }
        form = CrowdfundRequestForm(initial=initial)

    return render_to_response(
        'forms/foia/crowdfund.html',
        {'form': form},
        context_instance=RequestContext(request)
    )
