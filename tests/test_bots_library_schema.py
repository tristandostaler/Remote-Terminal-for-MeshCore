async def test_seeding_preserves_operator_modified_bot_schema(test_db):
    from app.bots.library import ensure_seeded, get_library_entry
    from app.repository.bots import BotRepository

    entry = get_library_entry("sms")
    assert entry is not None
    custom_schema = [{"key": "operator_field", "type": "text"}]
    record = await BotRepository.create(
        name="SMS customized",
        code=entry["code"],
        settings_schema=custom_schema,
        builtin_key="sms",
        builtin_version="0.0.1",
        modified=True,
    )

    await ensure_seeded()

    refreshed = await BotRepository.get(record.id)
    assert refreshed is not None
    assert refreshed.settings_schema == custom_schema
    assert refreshed.builtin_version == "0.0.1"


async def test_a_version_bump_is_what_delivers_new_code(test_db):
    """Seeding compares versions, never code — so an edit without a bump is
    invisible to every install that already has the bot.

    This is the trap: a library file can be changed, shipped, and never reach a
    single unmodified row. Both halves are asserted here so the coupling is
    visible to whoever edits a bot next.
    """
    from app.bots.library import ensure_seeded, get_library_entry
    from app.repository.bots import BotRepository

    entry = get_library_entry("ping")
    assert entry is not None
    stale = (
        "from remoteterm import bot\n\n\n"
        '@bot.on_keyword("ping")\n'
        "async def signal_report(ctx, msg):\n"
        "    pass\n"
    )

    # An older version: refreshed to the shipped code, generic handler included.
    behind = await BotRepository.create(
        name="ping behind", code=stale, builtin_key="ping", builtin_version="0.0.1"
    )
    await ensure_seeded()
    refreshed = await BotRepository.get(behind.id)
    assert refreshed is not None
    assert refreshed.builtin_version == entry["version"]
    # Exactly what the library ships — which is how the generic keyword handler,
    # or any other library edit, reaches an install that already has the bot.
    assert refreshed.code == entry["code"], "the bump must carry the new code"

    await BotRepository.delete(behind.id)

    # The same version: stale code is left exactly as it is, forever.
    level = await BotRepository.create(
        name="ping level", code=stale, builtin_key="ping", builtin_version=entry["version"]
    )
    await ensure_seeded()
    unchanged = await BotRepository.get(level.id)
    assert unchanged is not None
    assert unchanged.code == stale, "no bump means no refresh, whatever the code says"
