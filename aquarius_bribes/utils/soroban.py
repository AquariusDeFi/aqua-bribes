from django.conf import settings

from stellar_sdk import Account, TransactionBuilder
from stellar_sdk import xdr as stellar_xdr

from aquarius_bribes.utils.rpc import get_rpc_server


class TokenSymbolError(Exception):
    pass


def get_token_symbol(contract_id: str) -> str:
    """Read the SEP-41 token symbol directly from the contract via simulation."""
    server = get_rpc_server()
    # Simulation does not validate the sequence number, so a plain Account
    # object is enough — no need to load it from the network.
    source = Account(settings.BRIBE_WALLET_ADDRESS, 0)

    transaction = (
        TransactionBuilder(
            source_account=source,
            network_passphrase=settings.STELLAR_PASSPHRASE,
            base_fee=settings.BASE_FEE,
        )
        .append_invoke_contract_function_op(
            contract_id=contract_id,
            function_name='symbol',
            parameters=[],
        )
        .set_timeout(30)
        .build()
    )

    simulated = server.simulate_transaction(transaction)
    if simulated.error or not simulated.results:
        raise TokenSymbolError('Simulation failed for {}: {}'.format(contract_id, simulated.error))

    sc_val = stellar_xdr.SCVal.from_xdr(simulated.results[0].xdr)
    if sc_val.type == stellar_xdr.SCValType.SCV_STRING:
        return sc_val.str.sc_string.decode('utf-8')
    if sc_val.type == stellar_xdr.SCValType.SCV_SYMBOL:
        return sc_val.sym.sc_symbol.decode('utf-8')

    raise TokenSymbolError('Unexpected symbol() result type for {}: {}'.format(contract_id, sc_val.type))
