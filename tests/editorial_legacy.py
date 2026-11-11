"""Pin pre-existing offline fixtures to their original v12 payload contract.

Native fake-provider setup emits current schemas; explicit legacy cases keep
exercising their unchanged source/date/canonical rules without invented fields.
"""

from healthlab import editorial as ed


def canonical_v12(*args, **kwargs):
    return ed.canonical_proposal(*args, version="gold-editorial-12", **kwargs)


def resolve_v12(*args, **kwargs):
    return ed.resolve_reference_ids(*args, version="gold-editorial-12", **kwargs)
