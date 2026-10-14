from functools import wraps

from flask import abort
from flask_login import current_user


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def department_required(view):
    """Any logged-in user with a department assigned (i.e. not an admin-only
    account). Admins are also let through so they can preview department
    pages read-only, scoped to their own records only via query params --
    the department blueprint itself decides what an admin may see."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(403)
        if current_user.department_id is None and not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped
