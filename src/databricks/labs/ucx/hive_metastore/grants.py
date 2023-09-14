from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial

from databricks.labs.ucx.framework.crawlers import CrawlerBase
from databricks.labs.ucx.framework.parallel import ThreadedExecution
from databricks.labs.ucx.hive_metastore.tables import TablesCrawler


@dataclass(frozen=True)
class Grant:
    principal: str
    action_type: str
    catalog: str
    database: str = None
    table: str = None
    view: str = None
    any_file: bool = False
    anonymous_function: bool = False

    @staticmethod
    def type_and_key(
        *,
        catalog: str | None = None,
        database: str | None = None,
        table: str | None = None,
        view: str | None = None,
        any_file: bool = False,
        anonymous_function: bool = False,
    ) -> (str, str):
        if table is not None:
            catalog = "hive_metastore" if catalog is None else catalog
            database = "default" if database is None else database
            return "TABLE", f"{catalog}.{database}.{table}"
        if view is not None:
            catalog = "hive_metastore" if catalog is None else catalog
            database = "default" if database is None else database
            return "VIEW", f"{catalog}.{database}.{view}"
        if database is not None:
            catalog = "hive_metastore" if catalog is None else catalog
            return "DATABASE", f"{catalog}.{database}"
        if any_file:
            return "ANY FILE", ""
        if anonymous_function:
            return "ANONYMOUS FUNCTION", ""
        # Must come last, as it has lowest priority here but is a required parameter
        if catalog is not None:
            return "CATALOG", catalog
        msg = "invalid grant keys"
        raise ValueError(msg)

    @property
    def object_key(self) -> str:
        _, key = self._this_type_and_key()
        return key.lower()

    def _this_type_and_key(self):
        return self.type_and_key(
            catalog=self.catalog,
            database=self.database,
            table=self.table,
            view=self.view,
            any_file=self.any_file,
            anonymous_function=self.anonymous_function,
        )

    def hive_grant_sql(self) -> str:
        object_type, object_key = self._this_type_and_key()
        return f"GRANT {self.action_type} ON {object_type} {object_key} TO {self.principal}"

    def hive_revoke_sql(self) -> str:
        object_type, object_key = self._this_type_and_key()
        return f"REVOKE {self.action_type} ON {object_type} {object_key} FROM {self.principal}"

    def _set_owner(self, object_type, object_key):
        return f"ALTER {object_type} {object_key} OWNER TO {self.principal}"

    def _uc_action(self, action_type):
        def inner(object_type, object_key):
            return f"GRANT {action_type} ON {object_type} {object_key} TO {self.principal}"

        return inner

    def uc_grant_sql(self):
        """Get SQL translated SQL statement for granting similar permissions in UC.

        If there's no UC equivalent, returns None. This can also be the case for missing mapping.
        """

        # TODO: verify and complete the mapping
        # See: https://docs.databricks.com/sql/language-manual/sql-ref-privileges-hms.html
        # See: https://docs.databricks.com/data-governance/unity-catalog/manage-privileges/ownership.html
        # See: https://docs.databricks.com/data-governance/unity-catalog/manage-privileges/privileges.html
        object_type, object_key = self._this_type_and_key()
        hive_to_uc = {
            ("FUNCTION", "SELECT"): self._uc_action("EXECUTE"),
            ("TABLE", "SELECT"): self._uc_action("SELECT"),
            ("TABLE", "MODIFY"): self._uc_action("MODIFY"),
            ("TABLE", "READ_METADATA"): self._uc_action("BROWSE"),
            ("TABLE", "OWN"): self._set_owner,
            ("DATABASE", "USAGE"): self._uc_action("USE SCHEMA"),
            ("DATABASE", "CREATE"): self._uc_action("CREATE TABLE"),
            ("DATABASE", "CREATE_NAMED_FUNCTION"): self._uc_action("CREATE FUNCTION"),
            ("DATABASE", "SELECT"): self._uc_action("SELECT"),
            ("DATABASE", "MODIFY"): self._uc_action("MODIFY"),
            ("DATABASE", "OWN"): self._set_owner,
            ("DATABASE", "READ_METADATA"): self._uc_action("BROWSE"),
            ("CATALOG", "OWN"): self._set_owner,
        }
        make_query = hive_to_uc.get((object_type, self.action_type), None)
        if make_query is None:
            # unknown mapping or ignore
            return None
        return make_query(object_type, object_key)


class GrantsCrawler(CrawlerBase):
    def __init__(self, tc: TablesCrawler):
        super().__init__(tc._backend, tc._catalog, tc._schema, "grants")
        self._tc = tc

    def snapshot(self, catalog: str, database: str) -> list[Grant]:
        return self._snapshot(
            Grant, partial(self._try_load, catalog, database), partial(self._crawl, catalog, database)
        )

    def _try_load(self, catalog: str, database: str):
        for row in self._fetch(
            f'SELECT * FROM {self._full_name} WHERE catalog = "{catalog}" AND database = "{database}"'
        ):
            yield Grant(*row)

    def _crawl(self, catalog: str, database: str) -> list[Grant]:
        """
        Crawls and lists grants for tables and views within the specified catalog and database.

        Args:
            catalog (str): The catalog name.
            database (str): The database name.

        Returns:
            list[Grant]: A list of Grant objects representing the listed grants.

        Behavior:
        - Validates and prepares the provided catalog and database names.
        - Constructs a list of tasks to fetch grants using the `_grants` method, including both database-wide and
          table/view-specific grants.
        - Iterates through tables in the specified database using the `_tc.snapshot` method.
        - For each table, adds tasks to fetch grants for the table or its view, depending on the kind of the table.
        - Executes the tasks concurrently using ThreadedExecution.gather.
        - Flattens the list of retrieved grant lists into a single list of Grant objects.

        Note:
        - The method assumes that the `_grants` method fetches grants based on the provided parameters (catalog,
          database, table, view).

        Returns:
        list[Grant]: A list of Grant objects representing the grants found in the specified catalog and database.
        """
        catalog = self._valid(catalog)
        database = self._valid(database)
        tasks = [partial(self._grants, catalog=catalog), partial(self._grants, catalog=catalog, database=database)]
        for table in self._tc.snapshot(catalog, database):
            fn = partial(self._grants, catalog=catalog, database=database)
            if table.kind == "VIEW":
                tasks.append(partial(fn, view=table.name))
            else:
                tasks.append(partial(fn, table=table.name))
        return [
            grant
            for grants in ThreadedExecution.gather(f"listing grants for {catalog}.{database}", tasks)
            for grant in grants
        ]

    def _grants(
        self,
        *,
        catalog: str = False,
        database: str | None = None,
        table: str | None = None,
        view: str | None = None,
        any_file: bool = False,
        anonymous_function: bool = False,
    ) -> Iterator[Grant]:
        """
        Fetches and yields grant information for the specified database objects.

        Keyword Args:
            catalog (str): The catalog name (optional).
            database (str | None): The database name (optional).
            table (str | None): The table name (optional).
            view (str | None): The view name (optional).
            any_file (bool): Whether to include any file grants (optional).
            anonymous_function (bool): Whether to include anonymous function grants (optional).

        Yields:
            Iterator[Grant]: An iterator of Grant objects representing the fetched grants.

        Behavior:
        - Normalizes the provided parameters and constructs an object type and key using
          the `Grant.type_and_key` method.
        - Iterates through rows fetched using the `_fetch` method by executing a SQL query
          to retrieve grant information.
        - For each fetched row, extracts the principal, action type, and object type.
        - Normalizes the object type and filters grants based on the provided object type
          and the fetched object type.
        - Yields a Grant object representing the fetched grant information.

        Note:
        - The method fetches and yields grants based on the provided parameters and
          the available grant information in the database.

        Returns:
        Iterator[Grant]: An iterator of Grant objects representing the fetched grant information.
        """
        on_type, key = Grant.type_and_key(
            catalog=self._try_valid(catalog),
            database=self._try_valid(database),
            table=self._try_valid(table),
            view=self._try_valid(view),
            any_file=any_file,
            anonymous_function=anonymous_function,
        )
        object_type_normalization = {"SCHEMA": "DATABASE", "CATALOG$": "CATALOG"}
        for row in self._fetch(f"SHOW GRANTS ON {on_type} {key}"):
            (principal, action_type, object_type, _) = row
            if object_type in object_type_normalization:
                object_type = object_type_normalization[object_type]
            if on_type != object_type:
                continue
            yield Grant(
                principal=principal,
                action_type=action_type,
                table=table,
                view=view,
                database=database,
                catalog=catalog,
                any_file=any_file,
                anonymous_function=anonymous_function,
            )