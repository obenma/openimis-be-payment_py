from django.db.models import Q
from django.core.exceptions import PermissionDenied
from graphene_django.filter import DjangoFilterConnectionField
import graphene_django_optimizer as gql_optimizer
from payment.services import detach_payment_detail

from .apps import PaymentConfig
from django.utils.translation import gettext as _
from core.schema import signal_mutation_module_before_mutating, signal_mutation_module_after_mutating, \
    OrderedDjangoFilterConnectionField
from core.gql import legacy_row_filters, row_filter_arguments
from core.models import Audits, Validity
from contribution import models as contribution_models
from .models import Payment, PaymentDetail
# We do need all queries and mutations in the namespace here.
from .gql_queries import *  # lgtm [py/polluting-import]
from .gql_mutations import *  # lgtm [py/polluting-import]
from .signals import signal_before_payment_query, _read_signal_results


class Query(graphene.ObjectType):
    payments = OrderedDjangoFilterConnectionField(
        PaymentGQLType,
        **row_filter_arguments(),
        show_reconciled=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        orderBy=graphene.List(of_type=graphene.String),
        # OFS-257: Create dynamic filters for the payment mutation
        additional_filter=graphene.JSONString(),
    )
    payment_details = OrderedDjangoFilterConnectionField(
        PaymentDetailGQLType,
        **row_filter_arguments(),
        orderBy=graphene.List(of_type=graphene.String),
    )
    payments_by_premiums = OrderedDjangoFilterConnectionField(
        PaymentGQLType,
        premium_uuids=graphene.List(graphene.String, required=True),
        **row_filter_arguments(),
        orderBy=graphene.List(of_type=graphene.String),
    )

    def resolve_payments(self, info, **kwargs):
        if not info.context.user.has_perms(PaymentConfig.gql_query_payments_perms):
            raise PermissionDenied(_("unauthorized"))
        filters = []
        # OFS-257: Create dynamic filters for the payment mutation
        additional_filter = kwargs.get('additional_filter', None)
        # go to process additional filter only when this arg of filter was passed into query
        if additional_filter:
            filters_from_signal = _get_additional_filter(
                sender=Payment, additional_filter=additional_filter, user=info.context.user
            )
            # check if there is filter from signal (perms will be checked in the signals)
            if len(filters_from_signal) == 0:
                raise PermissionDenied(_("unauthorized"))
            filters.extend(filters_from_signal)
        if not info.context.user.has_perms(PaymentConfig.gql_query_payments_perms):
            raise PermissionDenied(_("unauthorized"))
        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            filters.append(
                Q(mutations__mutation__client_mutation_id=client_mutation_id))
        # validity / audits are applied by Payment.get_queryset (OP-2029), and so is the
        # row scope (PaymentGQLType); showHistory and a lookup by uuid keep their legacy
        # meaning: no validity filter at all
        everything = kwargs.get('show_history', False) or kwargs.get('uuid', None)
        legacy_row_filters(
            info, kwargs,
            validity=Validity.ALL if everything else Validity.NOW,
            audits=Audits.ALL if everything else None)
        show_reconciled = kwargs.get('show_reconciled', False)
        if show_reconciled:
            filters.append(
                Q(reconciliation_date__isnull=False)
            )

        return gql_optimizer.query(Payment.objects.filter(*filters).distinct().all(), info)

    def resolve_payment_details(self, info, **kwargs):
        if not info.context.user.has_perms(PaymentConfig.gql_query_payments_perms):
            raise PermissionDenied(_("unauthorized"))
        # it returned None, i.e. the whole table: the details are now narrowed to the
        # premiums the user may see by PaymentDetail.get_queryset (PaymentDetailGQLType),
        # current ones by default (OP-2029)
        legacy_row_filters(info, kwargs, validity=Validity.NOW)
        return PaymentDetail.objects.all()

    def resolve_payments_by_premiums(self, info, **kwargs):
        if not info.context.user.has_perms(PaymentConfig.gql_query_payments_perms):
            raise PermissionDenied(_("unauthorized"))
        # only the premiums the user may see (a deleted one included, as before); the
        # payments themselves are narrowed by Payment.get_queryset (PaymentGQLType)
        user = info.context.user
        premiums = contribution_models.Premium.get_queryset(
            contribution_models.Premium.objects.filter(uuid__in=kwargs.get('premium_uuids')),
            user, validity=Validity.ALL).values('id')
        detail_ids = PaymentDetail.get_queryset(
            PaymentDetail.objects.filter(premium_id__in=premiums), user).values('payment_id')
        legacy_row_filters(info, kwargs, validity=Validity.NOW)
        return Payment.objects.filter(Q(id__in=detail_ids))


class Mutation(graphene.ObjectType):
    create_payment = CreatePaymentMutation.Field()
    update_payment = UpdatePaymentMutation.Field()
    delete_payment = DeletePaymentsMutation.Field()


def bind_signals():
    # after the policy mutation, which checked its rights and scope: the payment details
    # of the policies it did delete are detached (it ran before, for any policy named)
    signal_mutation_module_after_mutating["policy"].connect(on_policy_mutation)
    signal_mutation_module_before_mutating["payment"].connect(on_payment_mutation)


def _get_additional_filter(sender, additional_filter, user):
    # function to retrieve additional filters from signal
    filters_from_signal = []
    if additional_filter:
        # send signal to append additional filter
        results_signal = signal_before_payment_query.send(
            sender=sender, additional_filter=additional_filter, user=user,
        )
        filters_from_signal = _read_signal_results(results_signal)
    return filters_from_signal
