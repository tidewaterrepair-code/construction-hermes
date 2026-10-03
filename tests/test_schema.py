import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def test_migrations_apply_and_app_role_is_least_privilege(session, owner):
    from chops.services import audit
    audit.record(session, owner, "test.event")
    session.commit()
    # The runtime role cannot rewrite audit history...
    with pytest.raises(DBAPIError):
        session.execute(text("UPDATE audit_events SET action='x'"))
    session.rollback()
    with pytest.raises(DBAPIError):
        session.execute(text("DELETE FROM audit_events"))
    session.rollback()
    # ...or change the schema.
    with pytest.raises(DBAPIError):
        session.execute(text("CREATE TABLE evil(id int)"))
    session.rollback()
    with pytest.raises(DBAPIError):
        session.execute(text("DROP TABLE leads"))
    session.rollback()
