"""Add deterministic CJK bigram FTS for D7 keyword retrieval."""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_d7_cjk_fts"
down_revision: str | None = "0008_d7_retrieval_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE FUNCTION retrieval_fts_lexemes(value text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        RETURN value || ' ' || COALESCE((
          SELECT string_agg(character || next_character, ' ' ORDER BY ordinal)
          FROM (
            SELECT character,
                   lead(character) OVER (ORDER BY ordinal) AS next_character,
                   ordinal
            FROM regexp_split_to_table(value, '')
              WITH ORDINALITY AS characters(character, ordinal)
          ) pairs
          WHERE ascii(character) BETWEEN 19968 AND 40959
            AND ascii(next_character) BETWEEN 19968 AND 40959
        ), '')
        """
    )
    op.execute(
        r"""
        CREATE FUNCTION retrieval_fts_query(value text)
        RETURNS tsquery
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        RETURN (
          SELECT to_tsquery('simple', string_agg(quote_literal(lexeme), ' | '))
          FROM unnest(
            tsvector_to_array(to_tsvector('simple', retrieval_fts_lexemes(value)))
          ) AS lexemes(lexeme)
        )
        """
    )
    op.execute("DROP INDEX ix_retrieval_chunks_search")
    op.execute("ALTER TABLE retrieval_chunks DROP COLUMN search_vector")
    op.execute(
        "ALTER TABLE retrieval_chunks ADD COLUMN search_vector tsvector "
        "GENERATED ALWAYS AS "
        "(to_tsvector('simple', retrieval_fts_lexemes(text))) STORED"
    )
    op.execute(
        "CREATE INDEX ix_retrieval_chunks_search "
        "ON retrieval_chunks USING gin(search_vector)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_retrieval_chunks_search")
    op.execute("ALTER TABLE retrieval_chunks DROP COLUMN search_vector")
    op.execute(
        "ALTER TABLE retrieval_chunks ADD COLUMN search_vector tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED"
    )
    op.execute(
        "CREATE INDEX ix_retrieval_chunks_search "
        "ON retrieval_chunks USING gin(search_vector)"
    )
    op.execute("DROP FUNCTION retrieval_fts_query(text)")
    op.execute("DROP FUNCTION retrieval_fts_lexemes(text)")
