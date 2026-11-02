"""Worker (and catalog) names: one word, free, and stable.

`suggest` picks a free single word from a built-in list of Greek-mythology and
constellation names. The pick is seeded from the set of taken names, not a
clock or RNG, so re-reading never reshuffles; it moves on only once the
suggested name is taken.

Workers get a name at instance reserve (`instances.agent_name`); the catalog
also uses the same picker for a suggested *pool* name.
"""

import hashlib

import aiosqlite

_WORDS = """
    achilles adonis aeolus aether ajax alcyone amphion anteros apollo ares argo
    ariadne artemis asteria astraeus athena atlas atropos boreas briareus
    calliope calypso cassandra chaos charon chiron circe clio clymene coeus
    cronus daedalus demeter dione eos erato eris eros euterpe gaia hades
    harmonia hebe hecate hecaton helios hephaestus hera hermes hestia hyperion
    hypnos iapetus icarus iris jason kratos leto lethe maia medusa metis minos
    mnemosyne morpheus nemesis nereus nike nyx oceanus odysseus orpheus pallas
    pan pandora panoptes persephone perseus phoebe phorcys pontus poseidon
    prometheus proteus psyche rhea selene semele tethys thalia thanatos theia
    themis thetis triton tyche urania uranus zephyr zeus alcor aldebaran algol
    altair andromeda antares aquila arcturus aries auriga betelgeuse bootes
    caelum canopus capella carina cassiopeia centaurus cepheus cetus columba
    corvus crux cygnus delphinus draco deneb eridanus fomalhaut fornax gemini
    grus hercules hydrus lacerta leo lepus libra lupus lynx lyra mensa mizar
    octans ophiuchus orion pavo pegasus phoenix pictor pisces polaris procyon
    puppis pyxis regulus reticulum rigel sagitta scorpius scutum serpens sextans
    sirius spica taurus tucana ursa vega vela virgo volans vulpecula
"""
WORDS = tuple(_WORDS.split())


def suggest(taken: set[str]) -> str:
    """A free single word, deterministic for this set of taken names."""
    used = sorted(taken.intersection(WORDS))  # suffixed names must not move the base
    seed = hashlib.sha256("\n".join(used).encode()).digest()
    start = int.from_bytes(seed[:4]) % len(WORDS)
    for i in range(len(WORDS)):
        word = WORDS[(start + i) % len(WORDS)]
        if word not in taken:
            return word
    n = 2
    while f"{WORDS[start]}-{n}" in taken:
        n += 1
    return f"{WORDS[start]}-{n}"


async def taken_worker_names(conn: aiosqlite.Connection) -> set[str]:
    """Names that must not be reassigned: live agents, pending instances, pools."""
    taken: set[str] = set()
    async with conn.execute(
        "SELECT name FROM agents WHERE state IN ('active', 'draining')"
    ) as cur:
        taken.update(r[0] for r in await cur.fetchall() if r[0])
    async with conn.execute(
        "SELECT agent_name FROM instances"
        " WHERE state != 'terminated' AND agent_name IS NOT NULL"
    ) as cur:
        taken.update(r[0] for r in await cur.fetchall())
    async with conn.execute("SELECT name FROM pools") as cur:
        taken.update(r[0] for r in await cur.fetchall())
    return taken
