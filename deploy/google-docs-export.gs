/**
 * DELEG8 -> Google Docs exporter. A tiny Apps Script web app.
 *
 * Why it exists: Google's "new doc" link cannot be pre-filled, and writing a Doc
 * as the viewer needs them to sign in with Google. This runs as YOU instead: the
 * server POSTs the briefing here, a real Doc is created in your Drive with the
 * text already in it, shared view-only by link, and its URL is returned.
 *
 * Set up (about 3 minutes):
 *   1. script.new  ->  paste this file  ->  set SECRET below to a long random string.
 *   2. Deploy -> New deployment -> type "Web app"
 *        Execute as: Me          Who has access: Anyone
 *      Authorise the Drive/Docs prompts, then copy the /exec URL.
 *   3. In .env:   DELEG8_GDOC_WEBAPP_URL=<the /exec URL>
 *                 DELEG8_GDOC_WEBAPP_SECRET=<the same SECRET>
 *      and restart the API.
 *
 * Docs are created in your Drive, visible to anyone with the link. Use it for demo
 * content only; the secret stops strangers from using the endpoint to create docs.
 */
var SECRET = 'change-me-to-a-long-random-string';

function doPost(e) {
  var d = JSON.parse(e.postData.contents);
  if (!SECRET || d.secret !== SECRET) return out_({ error: 'forbidden' });

  var doc = DocumentApp.create(d.title);
  var body = doc.getBody();
  var H = DocumentApp.ParagraphHeading;

  body.appendParagraph(d.title).setHeading(H.TITLE);
  body.appendParagraph(
    d.when + '  |  ' + d.minutes + ' min  |  With ' + ((d.participants || []).join(', ') || 'no one else')
  ).setForegroundColor('#666666');

  body.appendParagraph('Summary').setHeading(H.HEADING2);
  body.appendParagraph(d.summary);

  body.appendParagraph('Decisions').setHeading(H.HEADING2);
  bullets_(body, d.decisions, 'No decisions were made.');

  body.appendParagraph('Action items').setHeading(H.HEADING2);
  if (d.actions && d.actions.length) {
    var rows = [['Action item', 'Owner', 'Due']];
    d.actions.forEach(function (a) { rows.push([a.description, a.owner, a.due || '-']); });
    var table = body.appendTable(rows);
    table.getRow(0).editAsText().setBold(true);
  } else {
    body.appendParagraph('No action items.');
  }

  body.appendParagraph('Left open').setHeading(H.HEADING2);
  bullets_(body, d.open, 'Nothing left hanging.');

  body.appendParagraph('What you decided from your phone').setHeading(H.HEADING2);
  bullets_(
    body,
    (d.yours || []).map(function (y) { return y.topic + ': ' + y.outcome + ' ("' + y.heard + '")'; }),
    'Nothing needed your approval.'
  );

  body.appendParagraph('Written by DELEG8 for ' + d.principal + '.').setItalic(true).setForegroundColor('#888888');

  doc.saveAndClose();
  DriveApp.getFileById(doc.getId()).setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
  return out_({ url: 'https://docs.google.com/document/d/' + doc.getId() + '/edit' });
}

function bullets_(body, items, empty) {
  if (!items || !items.length) { body.appendParagraph(empty).setItalic(true); return; }
  items.forEach(function (t) { body.appendListItem(t).setGlyphType(DocumentApp.GlyphType.BULLET); });
}

function out_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
