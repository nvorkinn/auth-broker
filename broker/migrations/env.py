import logging
from logging.config import fileConfig

from alembic import context
from flask import current_app

config = context.config

# Only `flask db ...` configures logging here. When the app migrates itself on startup, fileConfig
# would disable the app's own logger (it disables every logger the ini file doesn't name).
if not config.attributes.get("app_startup"):
    fileConfig(config.config_file_name)
logger = logging.getLogger("alembic.env")

target_db = current_app.extensions["migrate"].db
config.set_main_option("sqlalchemy.url", target_db.engine.url.render_as_string(hide_password=False).replace("%", "%%"))


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"), target_metadata=target_db.metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Don't write an empty revision when autogenerate finds no schema changes.
    def process_revision_directives(context, revision, directives):
        if getattr(config.cmd_opts, "autogenerate", False):
            script = directives[0]
            if script.upgrade_ops.is_empty():
                directives[:] = []
                logger.info("No changes in schema detected.")

    # A copy: the app migrates itself on startup before any `flask db` command runs, and
    # storing this closure in the shared dict would pin it to that earlier run's config.
    conf_args = dict(current_app.extensions["migrate"].configure_args)
    conf_args.setdefault("process_revision_directives", process_revision_directives)

    with target_db.engine.connect() as connection:
        # SQLite alters a table by rebuilding it (batch mode), and dropping the old copy of a table
        # other rows reference fails while foreign keys are enforced. The pragma only takes effect
        # outside a transaction, hence the commits around it.
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.commit()
        try:
            context.configure(connection=connection, target_metadata=target_db.metadata, **conf_args)
            with context.begin_transaction():
                context.run_migrations()
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
