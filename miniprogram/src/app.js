const entryIntent = require('./lib/entry-intent');
App({ onShow(options) { entryIntent.receive(options && options.query); } });
