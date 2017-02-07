# -*- coding: utf-8 -*-
# Code for Life
#
# Copyright (C) 2016, Ocado Innovation Limited
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# ADDITIONAL TERMS – Section 7 GNU General Public Licence
#
# This licence does not grant any right, title or interest in any “Ocado” logos,
# trade names or the trademark “Ocado” or any other trademarks or domain names
# owned by Ocado Innovation Limited or the Ocado group of companies or any other
# distinctive brand features of “Ocado” as may be secured from time to time. You
# must not distribute any modification of this program using the trademark
# “Ocado” or claim any affiliation or association with Ocado or its employees.
#
# You are not authorised to use the name Ocado (or any of its trade names) or
# the names of any author or contributor in advertising or for publicity purposes
# pertaining to the distribution of this program, without the prior written
# authorisation of Ocado.
#
# Any propagation, distribution or conveyance of this program must include this
# copyright notice and these terms. You must not misrepresent the origins of this
# program; modified versions of the program must be marked as such and not
# identified as the original program.
from functools import partial
import json
from recaptcha import RecaptchaClient

from django.db.models import Q
from django.shortcuts import render, get_object_or_404
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.core.urlresolvers import reverse_lazy
from django.contrib import messages as messages
from django.contrib.auth import logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django_recaptcha_field import create_form_subclass_with_recaptcha

from portal import app_settings, emailMessages
from portal.models import School, Teacher, Class
from portal.forms.organisation import OrganisationJoinForm, OrganisationForm
from portal.forms.teach import ClassCreationForm, TeacherEditAccountForm
from portal.permissions import logged_in_as_teacher
from portal.helpers.emails_new import send_verification_email
from portal.helpers.generators import generate_access_code
from portal.helpers.location import lookup_coord

from portal.utils import using_two_factor


from ratelimit.decorators import ratelimit

recaptcha_client = RecaptchaClient(app_settings.RECAPTCHA_PRIVATE_KEY, app_settings.RECAPTCHA_PUBLIC_KEY)


@login_required(login_url=reverse_lazy('login_new'))
@user_passes_test(logged_in_as_teacher, login_url=reverse_lazy('login_new'))
def dashboard_teacher_view(request, is_admin):
    teacher = request.user.new_teacher
    school = teacher.school

    coworkers = Teacher.objects.filter(school=school).order_by('new_user__last_name', 'new_user__first_name')

    join_requests = Teacher.objects.filter(pending_join_request=school).order_by('new_user__last_name', 'new_user__first_name')

    update_school_form = OrganisationForm(user=request.user, current_school=school)
    update_school_form.fields['name'].initial = school.name
    update_school_form.fields['postcode'].initial = school.postcode
    update_school_form.fields['country'].initial = school.country
    create_class_form = ClassCreationForm()
    update_account_form = TeacherEditAccountForm(request.user)
    update_account_form.fields['title'].initial = teacher.title
    update_account_form.fields['first_name'].initial = request.user.first_name
    update_account_form.fields['last_name'].initial = request.user.last_name

    if can_process_forms(request, is_admin):
        if 'update_school' in request.POST:
            update_school_form = OrganisationForm(request.POST, user=request.user, current_school=school)
            process_update_school_form(request, school)

        elif 'create_class' in request.POST:
            create_class_form = ClassCreationForm(request.POST)
            process_create_class_form(request, teacher)

        else:
            update_account_form = TeacherEditAccountForm(request.user, request.POST)
            changing_email, new_email = process_update_account_form(request, teacher)
            if changing_email:
                logout(request)
                messages.success(request, 'Your account details have been successfully changed. Your email will be changed once you have verified it, until then you can still log in with your old email.')
                return render(request, 'redesign/email_verification_needed_new.html', {'userprofile': teacher.user, 'email': new_email})

    classes = Class.objects.filter(teacher=teacher)

    return render(request, 'redesign/teach_new/dashboard.html', {
        'teacher': teacher,
        'classes': classes,
        'is_admin': is_admin,
        'coworkers': coworkers,
        'join_requests': join_requests,
        'update_school_form': update_school_form,
        'create_class_form': create_class_form,
        'update_account_form': update_account_form,
    })


def can_process_forms(request, is_admin):
    return request.method == 'POST' and is_admin


def process_update_school_form(request, school):
    update_school_form = OrganisationForm(request.POST, user=request.user, current_school=school)
    if update_school_form.is_valid():
        data = update_school_form.cleaned_data
        name = data.get('name', '')
        postcode = data.get('postcode', '')
        country = data.get('country', '')

        school.name = name
        school.postcode = postcode
        school.country = country

        error, country, town, lat, lng = lookup_coord(postcode, country)
        school.town = town
        school.latitude = lat
        school.longitude = lng
        school.save()

        messages.success(request, 'You have updated the details for your school or club successfully.')


def process_create_class_form(request, teacher):
    create_class_form = ClassCreationForm(request.POST)
    if create_class_form.is_valid():
        created_class = create_class_new(create_class_form, teacher)
        messages.success(request, "The class '{className}' has been created successfully.".format(className=created_class.name))


def create_class_new(form, teacher):
    classmate_progress = False
    if form.cleaned_data['classmate_progress'] == 'True':
        classmate_progress = True
    klass = Class.objects.create(
        name=form.cleaned_data['class_name'],
        teacher=teacher,
        access_code=generate_access_code(),
        classmates_data_viewable=classmate_progress)
    return klass


def process_update_account_form(request, teacher):
    update_account_form = TeacherEditAccountForm(request.user, request.POST)
    changing_email = False
    new_email = ""
    if update_account_form.is_valid():
        data = update_account_form.cleaned_data
        changing_email = False

        # check not default value for CharField
        if (data['password'] != ''):
            teacher.new_user.set_password(data['password'])
            teacher.new_user.save()
            update_session_auth_hash(request, update_account_form.user)

        teacher.title = data['title']
        teacher.new_user.first_name = data['first_name']
        teacher.new_user.last_name = data['last_name']
        new_email = data['email']
        if new_email != '' and new_email != teacher.new_user.email:
            # new email to set and verify
            changing_email = True
            send_verification_email(request, teacher.new_user, new_email)

        teacher.save()
        teacher.new_user.save()

        messages.success(request, 'Your account details have been successfully changed.')

    return changing_email, new_email


@login_required(login_url=reverse_lazy('login_new'))
@user_passes_test(logged_in_as_teacher, login_url=reverse_lazy('login_new'))
def dashboard_manage(request):
    teacher = request.user.new_teacher

    if teacher.school:
        return dashboard_teacher_view(request, teacher.is_admin)
