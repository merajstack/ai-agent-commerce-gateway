require('dotenv').config();

module.exports = {
  PORT: process.env.PORT || 4000,
  STOREFRONT_URL: (process.env.STOREFRONT_URL || 'http://localhost:3000').replace(/\/$/, ''),
  GEMINI_API_KEY: process.env.GEMINI_API_KEY || '',
  GEMINI_MODEL: process.env.GEMINI_MODEL || 'gemini-3.5-flash',
  DEFAULT_PROTOCOL: 'acp',
  BUYER_IDENTITY: {
    agent_id: 'agent_buyer_007',
    first_name: 'Buyer',
    last_name: 'Agent',
    email: 'buyer@agentcommerce.ai'
  }
};
