/**
 * Gemini Function Calling Tool Definitions
 * Compact, precise schemas for shopping actions
 */
const toolDefinitions = [
  {
    functionDeclarations: [
      {
        name: "search_products",
        description: "Search merchant shoe catalog by query keywords, product name, or category filter.",
        parameters: {
          type: "OBJECT",
          properties: {
            query: {
              type: "STRING",
              description: "Search term such as 'AeroGlide', 'running', 'comfort', 'leather', 'waterproof', or product name"
            },
            category: {
              type: "STRING",
              description: "Product category filter: Running, Casual, Outdoor, Basketball, Formal",
              enum: ["Running", "Casual", "Outdoor", "Basketball", "Formal"]
            }
          }
        }
      },
      {
        name: "get_product",
        description: "Get comprehensive details of a specific product by its ID including price, stock, sizes, and specs.",
        parameters: {
          type: "OBJECT",
          properties: {
            id: {
              type: "STRING",
              description: "Product ID (e.g. 'prod_shoe_001')"
            }
          },
          required: ["id"]
        }
      },
      {
        name: "get_cart",
        description: "Fetch the current items, quantities, and subtotal in the user's shopping cart.",
        parameters: {
          type: "OBJECT",
          properties: {}
        }
      },
      {
        name: "add_to_cart",
        description: "Add a specific product with quantity and size to the merchant cart. Can be called multiple times for multiple items.",
        parameters: {
          type: "OBJECT",
          properties: {
            id: {
              type: "STRING",
              description: "Product ID (e.g. 'prod_shoe_001') or exact Product Name (e.g. 'AeroGlide Runner')"
            },
            quantity: {
              type: "INTEGER",
              description: "Quantity of items to add (e.g. 1, 2, 3)"
            },
            size: {
              type: "STRING",
              description: "Shoe size (e.g. '7', '8', '9', '10', '11')"
            }
          },
          required: ["id", "quantity"]
        }
      },
      {
        name: "update_cart",
        description: "Update the quantity of an item in the cart or remove it by setting quantity to 0.",
        parameters: {
          type: "OBJECT",
          properties: {
            id: {
              type: "STRING",
              description: "Product ID to update"
            },
            quantity: {
              type: "INTEGER",
              description: "New quantity (0 removes item from cart)"
            }
          },
          required: ["id", "quantity"]
        }
      },
      {
        name: "checkout",
        description: "Generate a protocol-compliant commerce request for all items in the cart and submit to the merchant backend for Gateway authorization.",
        parameters: {
          type: "OBJECT",
          properties: {
            confirmation: {
              type: "BOOLEAN",
              description: "Set to true to initiate checkout"
            }
          },
          required: ["confirmation"]
        }
      }
    ]
  }
];

module.exports = toolDefinitions;
