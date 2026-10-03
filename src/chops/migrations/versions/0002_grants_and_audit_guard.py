"""Least-privilege grants for the runtime role and append-only audit guard.

Revision ID: 0002
Revises: 0001
"""
import os

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("CHOPS_APP_DB_ROLE", "chops_app")


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION chops_audit_append_only() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'audit_events is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER audit_events_no_update BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION chops_audit_append_only();"
    )
    op.execute("CREATE TRIGGER audit_events_no_truncate BEFORE TRUNCATE ON audit_events "
               "FOR EACH STATEMENT EXECUTE FUNCTION chops_audit_append_only();")
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            EXECUTE 'GRANT USAGE ON SCHEMA public TO {APP_ROLE}';
            EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}';
            EXECUTE 'REVOKE UPDATE, DELETE ON audit_events FROM {APP_ROLE}';
            EXECUTE 'REVOKE ALL ON alembic_version FROM {APP_ROLE}';
            EXECUTE 'GRANT SELECT ON alembic_version TO {APP_ROLE}';
            EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}';
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_truncate ON audit_events")
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS chops_audit_append_only()")
