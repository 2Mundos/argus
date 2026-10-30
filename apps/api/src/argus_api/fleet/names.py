"""Suggested pool names: one word, free, and stable (worker launch 3).

The pick is seeded from the set of taken names, not a clock or RNG, so
re-reading the catalog never reshuffles the suggestion; it moves on only once
the suggested name is taken.
"""

import hashlib

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
