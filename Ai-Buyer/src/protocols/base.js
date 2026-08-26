/**
 * Base Protocol Definition Interface
 * Each protocol module implements generateRequest(cart, options)
 */
class BaseProtocol {
  constructor(id, name, version, status, description) {
    this.id = id;
    this.name = name;
    this.version = version;
    this.status = status; // 'supported' | 'sandbox' | 'experimental'
    this.description = description;
  }

  /**
   * Generates a protocol-compliant JSON request from cart data
   * @param {Object} cart - { items: [...], total_amount: number, currency: string }
   * @param {Object} options - { buyerIdentity, merchantId }
   * @returns {Object} raw protocol request payload
   */
  generateRequest(cart, options = {}) {
    throw new Error(`generateRequest must be implemented by ${this.name}`);
  }
}

module.exports = BaseProtocol;
