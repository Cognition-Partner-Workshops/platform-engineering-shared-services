"""SQL generation module with prompt engineering and safety validation."""

import logging
import re
from typing import Any, Optional

from llm_client import LLMClient

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a PostgreSQL expert. You receive natural language questions about "
    "a PostgreSQL database and return ONLY valid SQL SELECT queries. "
    "Rules:\n"
    "- Return ONLY the SQL query, nothing else.\n"
    "- Do NOT include explanations, comments, or markdown formatting.\n"
    "- Do NOT use DROP, DELETE, TRUNCATE, UPDATE, INSERT, ALTER, CREATE, or GRANT.\n"
    "- Only generate SELECT statements.\n"
    "- Always terminate the query with a semicolon.\n"
    "- If the question cannot be answered with a SELECT query, respond with: "
    "-- CANNOT_GENERATE"
)

DANGEROUS_KEYWORDS = frozenset(
    {
        "DROP",
        "DELETE",
        "TRUNCATE",
        "UPDATE",
        "INSERT",
        "ALTER",
        "CREATE",
        "GRANT",
        "REVOKE",
        "EXEC",
        "EXECUTE",
    }
)

MAX_RETRIES = 2


class SQLGenerationError(Exception):
    """Raised when SQL generation fails after retries."""


class UnsafeSQLError(Exception):
    """Raised when generated SQL contains dangerous operations."""


class SQLGenerator:
    """Generates safe SQL queries from natural language using an LLM."""

    def __init__(
        self,
        llm_client: LLMClient,
        schema_metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self.llm_client = llm_client
        self.schema_metadata = schema_metadata

    def update_schema(self, schema_metadata: dict[str, Any]) -> None:
        """Update the schema metadata used for prompt context.

        Args:
            schema_metadata: Dict mapping table names to column info lists.
        """
        self.schema_metadata = schema_metadata
        logger.info("Schema metadata updated: %d tables", len(schema_metadata))

    def generate_sql(self, user_query: str) -> str:
        """Generate a SQL query from a natural language question.

        Retries up to MAX_RETRIES times if validation fails.

        Args:
            user_query: The natural language question.

        Returns:
            A validated SQL SELECT query string.

        Raises:
            SQLGenerationError: If generation fails after all retries.
            UnsafeSQLError: If the generated SQL is unsafe.
        """
        prompt = self._build_prompt(user_query)

        last_error: Optional[str] = None
        for attempt in range(1, MAX_RETRIES + 1):
            logger.info("SQL generation attempt %d/%d", attempt, MAX_RETRIES)

            retry_prompt = prompt
            if last_error and attempt > 1:
                retry_prompt += (
                    f"\n\nPrevious attempt failed with error: {last_error}\n"
                    "Please generate a corrected SQL query."
                )

            try:
                raw_response = self.llm_client.generate(
                    prompt=retry_prompt,
                    system_prompt=SYSTEM_PROMPT,
                )
            except (ConnectionError, RuntimeError) as exc:
                logger.error("LLM request failed: %s", exc)
                raise SQLGenerationError(
                    f"Failed to communicate with LLM: {exc}"
                ) from exc

            sql = self._extract_sql(raw_response)

            if not sql or sql == "-- CANNOT_GENERATE":
                last_error = "LLM could not generate a valid query"
                logger.warning("Attempt %d: %s", attempt, last_error)
                continue

            try:
                self._validate_sql(sql)
                return sql
            except UnsafeSQLError:
                raise
            except ValueError as exc:
                last_error = str(exc)
                logger.warning("Attempt %d validation failed: %s", attempt, last_error)
                continue

        raise SQLGenerationError(
            f"Failed to generate valid SQL after {MAX_RETRIES} attempts. "
            f"Last error: {last_error}"
        )

    def _build_prompt(self, user_query: str) -> str:
        """Build the full prompt including schema context.

        Args:
            user_query: The natural language question.

        Returns:
            The complete prompt string.
        """
        parts = []

        if self.schema_metadata:
            parts.append("Database schema:")
            for table_name, columns in self.schema_metadata.items():
                col_defs = []
                for col in columns:
                    nullable = "NULL" if col["is_nullable"] == "YES" else "NOT NULL"
                    default = (
                        f" DEFAULT {col['column_default']}"
                        if col.get("column_default")
                        else ""
                    )
                    col_defs.append(
                        f"  {col['column_name']} {col['data_type']} {nullable}{default}"
                    )
                parts.append(f"TABLE {table_name} (\n" + ",\n".join(col_defs) + "\n)")
            parts.append("")

        parts.append(f"Question: {user_query}")
        parts.append("SQL:")

        return "\n".join(parts)

    @staticmethod
    def _extract_sql(raw_response: str) -> str:
        """Extract clean SQL from the LLM response.

        Strips markdown code blocks, comments, and extra whitespace.

        Args:
            raw_response: The raw LLM output.

        Returns:
            A cleaned SQL string.
        """
        text = raw_response.strip()

        # Remove markdown code fences
        code_block_match = re.search(
            r"```(?:sql)?\s*\n?(.*?)\n?```", text, re.DOTALL | re.IGNORECASE
        )
        if code_block_match:
            text = code_block_match.group(1).strip()

        # Remove leading/trailing comments
        lines = []
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped and not stripped.startswith("--"):
                lines.append(line)
            elif stripped == "-- CANNOT_GENERATE":
                return "-- CANNOT_GENERATE"

        sql = "\n".join(lines).strip()

        # Ensure trailing semicolon
        if sql and not sql.endswith(";"):
            sql += ";"

        return sql

    @staticmethod
    def _validate_sql(sql: str) -> None:
        """Validate that the SQL is a safe SELECT query.

        Args:
            sql: The SQL query to validate.

        Raises:
            UnsafeSQLError: If the query contains dangerous keywords.
            ValueError: If the query is not a valid SELECT statement.
        """
        if not sql:
            raise ValueError("Empty SQL query")

        normalized = sql.upper().strip()

        # Remove string literals to avoid false positives on keywords inside quotes
        sanitized = re.sub(r"'[^']*'", "''", normalized)

        # Check for dangerous keywords as standalone words
        for keyword in DANGEROUS_KEYWORDS:
            pattern = rf"\b{keyword}\b"
            if re.search(pattern, sanitized):
                raise UnsafeSQLError(
                    f"Unsafe SQL detected: query contains '{keyword}'. "
                    "Only SELECT queries are allowed."
                )

        # Verify it starts with SELECT or WITH (for CTEs)
        if not (sanitized.startswith("SELECT") or sanitized.startswith("WITH")):
            raise ValueError(
                f"Query must start with SELECT or WITH. Got: {sql[:50]}..."
            )
