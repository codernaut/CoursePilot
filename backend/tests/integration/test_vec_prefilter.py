"""The vec0 KNN query must filter by scope BEFORE applying k (not after)."""

from app.retrieval.hybrid import SearchScope, hybrid_search
from tests.ingest_support import Env
from tests.search_support import access_for_faculty, ingest, module_id


async def test_vector_side_returns_in_scope_rows_when_many_out_of_scope_are_closer(
    ingest_env: Env,
) -> None:
    env = ingest_env
    other = await module_id(env, 1, 1, 2)
    query = "tree traversal nodes"
    for i in range(40):
        await ingest(env, f"{query} {i}\n".encode(), title=f"T{i}")
    await ingest(
        env, b"Completely different words about oceans and tides.\n", title="Far", module=other
    )
    access = await access_for_faculty(env)
    vector = await env.embedder.embed_query(query)
    async with env.database.read_session() as db:
        hits = await hybrid_search(
            db, access=access, scope=SearchScope("module", other), query="???",
            query_vector=vector, top_k=5,
        )  # fmt: skip
    # "???" has no FTS tokens, so a hit can only come from the vector query
    assert [h.document_title for h in hits] == ["Far"]
    assert hits[0].vector_similarity is not None
