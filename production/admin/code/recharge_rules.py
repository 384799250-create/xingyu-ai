"""Server-owned recharge quotes. Persist each quote before opening checkout."""
VERSION = 'recharge-tiers-v1'
TIERS = ((100, 0), (500, 0), (1000, 300), (3000, 1500), (5000, 4000), (10000, 10000))
SUPPORTED_FEN = tuple(fen for fen, _ in TIERS)

def quote(fen):
    if type(fen) is not int or fen not in SUPPORTED_FEN:
        raise ValueError('unsupported_recharge_amount')
    bonus = dict(TIERS)[fen]
    return {'fen': fen, 'base_starcoins': fen * 10, 'bonus_starcoins': bonus,
            'starcoins': fen * 10 + bonus, 'pricing_version': VERSION}

def available(amounts):
    return [quote(fen) for fen in amounts]
