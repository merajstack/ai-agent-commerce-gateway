const acpProtocol = require('./acp');
const { x402Instance, validateX402Payload } = require('./x402');

/**
 * Protocol Registry
 * ACP (RFC 2026-01-16) and x402 v2 (HTTP 402 Payment Required) are active/supported.
 * AP2 is explicitly marked as unsupported ("Coming next").
 */
const protocols = {
  acp: acpProtocol,
  x402: x402Instance
};

const protocolMetadata = [
  {
    id: 'acp',
    name: 'ACP (Agentic Commerce Protocol)',
    version: '2026-01-16',
    status: 'supported',
    description: 'Authoritative Agentic Commerce Protocol (RFC 2026-01-16) with cryptographic session isolation.'
  },
  {
    id: 'x402',
    name: 'x402 v2 (HTTP 402 Pay-per-Request)',
    version: 'v2',
    status: 'supported',
    description: 'Authoritative x402 v2 PaymentPayload (Base Sepolia eip155:84532, exact scheme).'
  },
  {
    id: 'ap2',
    name: 'AP2 (Agent Payment Protocol)',
    version: 'v1.0.0-draft',
    status: 'unsupported',
    description: 'Coming next — Authoritative Mandate-driven AP2 adapter in progress.'
  }
];

function getProtocol(id) {
  const normalizedId = (id || 'acp').toLowerCase();
  return protocols[normalizedId] || null;
}

function listProtocols() {
  return protocolMetadata;
}

function generateProtocolPayload(protocolId, cart, options = {}) {
  const normalizedId = (protocolId || 'acp').toLowerCase();
  
  if (normalizedId === 'ap2') {
    const meta = protocolMetadata.find(p => p.id === 'ap2');
    return {
      status: 'unsupported',
      protocolId: 'ap2',
      protocolName: meta.name,
      protocolVersion: meta.version,
      payload: null,
      error: `${meta.name} is currently unsupported (Coming next). ACP (2026-01-16) and x402 v2 are the active supported protocols.`
    };
  }

  const protocolHandler = getProtocol(normalizedId);
  if (!protocolHandler) {
    return {
      status: 'unsupported',
      protocolId: normalizedId,
      protocolName: normalizedId.toUpperCase(),
      protocolVersion: 'N/A',
      payload: null,
      error: `Protocol "${normalizedId}" is not recognized or supported.`
    };
  }

  const rawPayload = protocolHandler.generateRequest(cart, options);

  if (normalizedId === 'x402') {
    validateX402Payload(rawPayload);
  }

  return {
    status: 'supported',
    protocolId: normalizedId,
    protocolName: protocolHandler.name,
    protocolVersion: protocolHandler.version,
    payload: rawPayload
  };
}

module.exports = {
  getProtocol,
  listProtocols,
  generateProtocolPayload,
  validateX402Payload
};
