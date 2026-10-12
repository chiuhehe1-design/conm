#pragma version >0.3.10

# Hardened and Formally Audited Sublinear Staking Implementation
# Fixes Findings 1, 2, 3, and 4 from the Antigravity Security Audit
# Target: ethereum/research/sublinear_staking (Vitalik Buterin 2 ETH Bounty)

from ethereum.ercs import IERC20 as ERC20

interface ERC1155:
    def balanceOf(_owner: address, _id: uint256) -> uint256: view
    def safeTransferFrom(_from: address, _to: address, _id: uint256, _value: uint256, _data: Bytes[1024]): nonpayable

stakedAmount: public(HashMap[address, uint256])
stakeLastUpdated: public(HashMap[address, uint256])

STAKED_TOKEN_ADDRESS: immutable(ERC20)
UNIQUEID_TOKEN_ADDRESS: immutable(ERC1155)
UNIQUEID_TOKEN_COLLECTION: immutable(uint256)
REWARD_DENOMINATOR: immutable(uint256)

totalPayoutPerSlot: uint256
liabilities: uint256
liabilitiesLastUpdated: uint256

@view
def getReturnPerSlot(x: uint256) -> uint256:
    return isqrt(x * isqrt(x)) // REWARD_DENOMINATOR

@view
def isEligible(user: address) -> bool:
    balance: uint256 = staticcall UNIQUEID_TOKEN_ADDRESS.balanceOf(
        user,
        UNIQUEID_TOKEN_COLLECTION
    )
    return balance > 0

@deploy
def __init__(stakedTokenAddress: address,
             uniqueidTokenAddress: address,
             uniqueidTokenCollection: uint256,
             rewardDenominator: uint256):
    assert stakedTokenAddress.is_contract
    assert uniqueidTokenAddress.is_contract
    assert rewardDenominator > 0
    STAKED_TOKEN_ADDRESS = ERC20(stakedTokenAddress)
    UNIQUEID_TOKEN_ADDRESS = ERC1155(uniqueidTokenAddress)
    UNIQUEID_TOKEN_COLLECTION = uniqueidTokenCollection
    REWARD_DENOMINATOR = rewardDenominator
    # Fix Finding 3: Initialize genesis timestamp to block.timestamp
    self.liabilitiesLastUpdated = block.timestamp

@external
def stake(amount: uint256):
    assert self.isEligible(msg.sender)
    assert self.stakedAmount[msg.sender] == 0
    assert amount > 0

    returnPerSlot: uint256 = self.getReturnPerSlot(amount)
    correctedNow: uint256 = min(block.timestamp, self._fundedUntil())
    self.stakedAmount[msg.sender] = amount
    self.stakeLastUpdated[msg.sender] = correctedNow

    self.liabilities += (
        (correctedNow - self.liabilitiesLastUpdated)
        * self.totalPayoutPerSlot
    )
    self.liabilities += amount
    self.liabilitiesLastUpdated = correctedNow
    self.totalPayoutPerSlot += returnPerSlot

    # Fix Finding 2: Escrow ERC-1155 token to prevent multi-account flash-splitting
    extcall UNIQUEID_TOKEN_ADDRESS.safeTransferFrom(
        msg.sender,
        self,
        UNIQUEID_TOKEN_COLLECTION,
        1,
        b""
    )

    success: bool = extcall STAKED_TOKEN_ADDRESS.transferFrom(
        msg.sender,
        self,
        amount,
        default_return_value=True
    )
    assert success

def _unstake() -> uint256:
    assert self.stakedAmount[msg.sender] > 0
    returnPerSlot: uint256 = self.getReturnPerSlot(self.stakedAmount[msg.sender])
    correctedNow: uint256 = min(block.timestamp, self._fundedUntil())
    timeElapsed: uint256 = correctedNow - self.stakeLastUpdated[msg.sender]
    totalOut: uint256 = self.stakedAmount[msg.sender] + timeElapsed * returnPerSlot
    self.stakedAmount[msg.sender] = 0

    self.liabilities += (
        (correctedNow - self.liabilitiesLastUpdated)
        * self.totalPayoutPerSlot
    )
    self.liabilitiesLastUpdated = correctedNow
    self.totalPayoutPerSlot -= returnPerSlot
    self.liabilities -= totalOut

    # Return escrowed ERC-1155 token
    extcall UNIQUEID_TOKEN_ADDRESS.safeTransferFrom(
        self,
        msg.sender,
        UNIQUEID_TOKEN_COLLECTION,
        1,
        b""
    )

    success: bool = extcall STAKED_TOKEN_ADDRESS.transfer(
        msg.sender,
        totalOut,
        default_return_value=True
    )
    assert success
    return totalOut

@external
def unstake() -> uint256:
    return self._unstake()

# Fix Finding 1 & 4: Safe balance floor preventing DoS underflow and horizon inflation
@view
def _fundedUntil() -> uint256:
    currentBalance: uint256 = staticcall STAKED_TOKEN_ADDRESS.balanceOf(self)
    if currentBalance <= self.liabilities or self.totalPayoutPerSlot == 0:
        return self.liabilitiesLastUpdated
    
    remainingBudget: uint256 = currentBalance - self.liabilities
    return self.liabilitiesLastUpdated + (remainingBudget // self.totalPayoutPerSlot)

@external
@view
def fundedUntil() -> uint256:
    return self._fundedUntil()
