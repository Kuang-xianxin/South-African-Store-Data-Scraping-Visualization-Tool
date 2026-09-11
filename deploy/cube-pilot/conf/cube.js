// Fail closed until the BLUE shadow model and row permissions are validated.
// This preparation does not enable production reads or export business data.
module.exports = {
  queryRewrite: () => {
    throw new Error('ERP Cube pilot model and store permissions are not enabled');
  },
  scheduledRefreshContexts: async () => [],
};
