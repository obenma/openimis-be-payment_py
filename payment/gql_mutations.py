from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError, PermissionDenied
from payment.apps import PaymentConfig
from payment.models import Payment, PaymentDetail, PaymentMutation
from policy import models as policy_models
from payment.services import update_or_create_payment, detach_payment_detail, set_payment_deleted, \
    update_or_create_payment_detail, scoped_payment, scoped_premium
from core.models import Audits, Validity
from typing import Optional

import graphene
from core.schema import OpenIMISMutation
from django.utils.translation import gettext_lazy, gettext as _


class PaymentBase:
    id = graphene.Int(required=False, read_only=True)
    uuid = graphene.String(required=False)
    expected_amount = graphene.Decimal(max_digits=18, decimal_places=2, required=False)
    received_amount = graphene.Decimal(max_digits=18, decimal_places=2, required=False)
    officer_code = graphene.String(required=False)
    phone_number = graphene.String(required=False)
    request_date = graphene.Date(required=False)
    received_date = graphene.Date(required=False)
    status = graphene.Int(required=False)
    transaction_no = graphene.String(required=False)
    origin = graphene.String(required=False)
    matched_date = graphene.Date(required=False)
    receipt_no = graphene.String(required=False)
    payment_date = graphene.Date(required=False)
    rejected_reason = graphene.String(required=False)
    date_last_sms = graphene.Date(required=False)
    language_name = graphene.String(required=False)
    type_of_payment = graphene.String(required=False)
    transfer_fee = graphene.Decimal(max_digits=18, decimal_places=2, required=False)
    premium_uuid = graphene.String(
        required=False, description=gettext_lazy("payment.gql.payment_base.premium_uuid"))


class CreatePaymentMutation(OpenIMISMutation):
    """
    Create a payment for policy with or without a payer
    """
    _mutation_module = "payment"
    _mutation_class = "CreatePaymentMutation"

    class Input(PaymentBase, OpenIMISMutation.Input):
        pass

    @classmethod
    def async_mutate(cls, user, **data) -> Optional[str]:
        try:
            if type(user) is AnonymousUser or not user.id:
                raise ValidationError(
                    _("mutation.authentication_required"))
            if not user.has_perms(PaymentConfig.gql_mutation_create_payments_perms):
                raise PermissionDenied(_("unauthorized"))
            premium_uuid = data.pop("premium_uuid") if "premium_uuid" in data else None
            # the premium is looked up within the user's scope before anything is written
            if premium_uuid and scoped_premium(user, premium_uuid) is None:
                raise PermissionDenied(_("unauthorized"))
            client_mutation_id = data.get("client_mutation_id")
            payment = update_or_create_payment(data, user, update=False)
            if premium_uuid:
                update_or_create_payment_detail(payment, premium_uuid, user)
            PaymentMutation.object_mutated(user, client_mutation_id=client_mutation_id, payment=payment)
            return None
        except Exception as exc:
            return [{
                'message': _("payment.mutation.failed_to_create_payment"),
                'detail': str(exc)}
            ]


class UpdatePaymentMutation(OpenIMISMutation):
    """
    Update a payment for policy
    """
    _mutation_module = "payment"
    _mutation_class = "UpdatePaymentMutation"

    class Input(PaymentBase, OpenIMISMutation.Input):
        pass

    @classmethod
    def async_mutate(cls, user, **data) -> Optional[str]:
        try:
            if type(user) is AnonymousUser or not user.id:
                raise ValidationError(
                    _("mutation.authentication_required"))
            if not user.has_perms(PaymentConfig.gql_mutation_update_payments_perms):
                raise PermissionDenied(_("unauthorized"))
            premium_uuid = data.pop("premium_uuid") if "premium_uuid" in data else None
            # the payment updated and the premium linked: within the user's scope
            if premium_uuid and scoped_premium(user, premium_uuid) is None:
                raise PermissionDenied(_("unauthorized"))
            payment = update_or_create_payment(data, user, update=True)
            if premium_uuid:
                update_or_create_payment_detail(payment, premium_uuid, user)
            return None
        except Exception as exc:
            return [{
                'message': _("payment.mutation.failed_to_update_payment") % {'id': data.get('uuid') if data else None},
                'detail': str(exc)
            }]


class DeletePaymentsMutation(OpenIMISMutation):
    """
    Delete one or several Payments.
    """
    _mutation_module = "payment"
    _mutation_class = "DeletePaymentsMutation"

    class Input(OpenIMISMutation.Input):
        uuids = graphene.List(graphene.String)

    @classmethod
    def async_mutate(cls, user, **data):
        if not user.has_perms(PaymentConfig.gql_mutation_delete_payments_perms):
            raise PermissionDenied(_("unauthorized"))
        errors = []
        for payment_uuid in data["uuids"]:
            # a payment out of the user's scope (or already deleted) does not exist for them
            payment = scoped_payment(user, payment_uuid)
            if payment is None:
                errors.append({
                    'title': payment_uuid,
                    'list': [{'message': _(
                        "payment.validation.id_does_not_exist") % {'id': payment_uuid}}]
                })
                continue
            failure = set_payment_deleted(payment)
            if failure:
                errors.append(failure)
        if len(errors) == 1:
            errors = errors[0]['list']
        return errors


def on_policy_mutation(sender, **kwargs):
    """
    After a DeletePoliciesMutation (bound to the after-mutating signal: the policy
    mutation has checked its rights and scope): the current payment details of the
    premiums of the policies it did delete are detached from them. A policy it refused
    is still current and its payments stay matched.
    """
    errors = []
    if kwargs.get("mutation_class") == 'DeletePoliciesMutation':
        uuids = kwargs['data'].get('uuids', [])
        deleted = policy_models.Policy.objects.filter(uuid__in=uuids, validity_to__isnull=False)
        for payment_detail in PaymentDetail.objects.filter(
                premium__policy__in=deleted, validity_to__isnull=True):
            errors += detach_payment_detail(payment_detail)
    return errors


def on_payment_mutation(sender, **kwargs):
    uuids = kwargs['data'].get('uuids', [])
    if not uuids:
        uuid = kwargs['data'].get('uuid', None)
        uuids = [uuid] if uuid else []
    if not uuids:
        return []
    # only the payments the user may see get the mutation linked
    impacted_payments = Payment.get_queryset(
        Payment.objects.filter(uuid__in=uuids), kwargs.get('user'), validity=Validity.ALL, audits=Audits.ALL)
    for payment in impacted_payments:
        PaymentMutation.objects.get_or_create(payment=payment, mutation_id=kwargs['mutation_log_id'])
    return []
