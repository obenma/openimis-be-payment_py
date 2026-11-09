from django.apps import AppConfig

from core.rights_declaration import RightsDeclaration

MODULE_NAME = "payment"


# Rights, by entity then by action. The contract module deliberately reuses these
# same identifiers: they are the same actions on the same objects.
DJANGO_PERMS = {
    "payment": {
        "query": ("payment.view_payment", 101401),
        "create": ("payment.add_payment", 101402),
        "update": ("payment.change_payment", 101403),
        "delete": ("payment.delete_payment", 101404),
    },
}

_PERM_CFG = {
    "gql_query_payments_perms": ("payment", "query"),
    "gql_mutation_create_payments_perms": ("payment", "create"),
    "gql_mutation_update_payments_perms": ("payment", "update"),
    "gql_mutation_delete_payments_perms": ("payment", "delete"),
}

RIGHTS = RightsDeclaration(MODULE_NAME, DJANGO_PERMS, _PERM_CFG)

perms = RIGHTS.perms
django_perms = RIGHTS.django_perm_names
configured_perms = RIGHTS.configured
require = RIGHTS.require


DEFAULT_CFG = {
    "default_validations_disabled": False,
}


class PaymentConfig(AppConfig):
    name = MODULE_NAME

    # Rights: constants, no longer overridable. They go neither through DEFAULT_CFG
    # nor through ready(): `ModuleConfiguration.get_or_default` now ignores any
    # `_perms` key stored in the database.
    gql_query_payments_perms = RIGHTS.perms("payment", "query")
    gql_mutation_create_payments_perms = RIGHTS.perms("payment", "create")
    gql_mutation_update_payments_perms = RIGHTS.perms("payment", "update")
    gql_mutation_delete_payments_perms = RIGHTS.perms("payment", "delete")
    default_validations_disabled = None

    def __load_config(self, cfg):
        for field in cfg:
            if hasattr(PaymentConfig, field):
                setattr(PaymentConfig, field, cfg[field])

    def ready(self):
        from core.models import ModuleConfiguration
        cfg = ModuleConfiguration.get_or_default(MODULE_NAME, DEFAULT_CFG)
        self.__load_config(cfg)
