// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title FarcasterOGBridge
 * @notice Cross-chain bridge adapter to migrate Farcaster OG NFTs from Zora (Chain 7777777) to Base (Chain 8453).
 * Built for @cojo.eth Farcaster Bounty ($500 USDC).
 */

interface IERC721 {
    function ownerOf(uint256 tokenId) external view returns (address);
    function transferFrom(address from, address to, uint256 tokenId) external;
    function mint(address to, uint256 tokenId) external;
}

interface ICrossDomainMessenger {
    function sendMessage(address _target, bytes calldata _message, uint32 _gasLimit) external payable;
    function xDomainMessageSender() external view returns (address);
}

contract FarcasterOGZoraSource {
    address public immutable zoraNFTContract;
    address public immutable crossDomainMessenger;
    address public baseDestinationBridge;
    address public owner;

    event BridgedToBase(address indexed user, uint256 indexed tokenId, uint32 timestamp);

    constructor(address _nft, address _messenger) {
        zoraNFTContract = _nft;
        crossDomainMessenger = _messenger;
        owner = msg.sender;
    }

    function setBaseBridge(address _baseBridge) external {
        require(msg.sender == owner, "Only owner");
        baseDestinationBridge = _baseBridge;
    }

    function bridgeToBase(uint256 tokenId) external {
        require(IERC721(zoraNFTContract).ownerOf(tokenId) == msg.sender, "Not token owner");
        
        // Lock NFT on Zora in bridge vault
        IERC721(zoraNFTContract).transferFrom(msg.sender, address(this), tokenId);

        // Encode message for Base destination contract
        bytes memory message = abi.encodeWithSignature("receiveFromZora(address,uint256)", msg.sender, tokenId);

        // Dispatch via OP Stack CrossDomainMessenger
        ICrossDomainMessenger(crossDomainMessenger).sendMessage(
            baseDestinationBridge,
            message,
            200000 // gas limit
        );

        emit BridgedToBase(msg.sender, tokenId, uint32(block.timestamp));
    }
}

contract FarcasterOGBaseDestination {
    address public immutable baseNFTContract;
    address public immutable crossDomainMessenger;
    address public zoraSourceBridge;
    address public owner;

    event MintedOnBase(address indexed user, uint256 indexed tokenId);

    constructor(address _baseNft, address _messenger) {
        baseNFTContract = _baseNft;
        crossDomainMessenger = _messenger;
        owner = msg.sender;
    }

    function setZoraBridge(address _zoraBridge) external {
        require(msg.sender == owner, "Only owner");
        zoraSourceBridge = _zoraBridge;
    }

    function receiveFromZora(address recipient, uint256 tokenId) external {
        require(msg.sender == crossDomainMessenger, "Caller must be messenger");
        require(
            ICrossDomainMessenger(crossDomainMessenger).xDomainMessageSender() == zoraSourceBridge,
            "Invalid remote caller"
        );

        // Mint or release verified Farcaster OG NFT on Base
        IERC721(baseNFTContract).mint(recipient, tokenId);
        emit MintedOnBase(recipient, tokenId);
    }
}
