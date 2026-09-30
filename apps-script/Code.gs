const MAX_IMAGE_BYTES = 2 * 1000 * 1000;

function doPost(event) {
  try {
    const data = JSON.parse(event.postData.contents);
    const expectedSecret = PropertiesService.getScriptProperties()
      .getProperty('UPLOAD_SECRET');
    if (!expectedSecret || data.secret !== expectedSecret) {
      throw new Error('Unauthorized');
    }

    const row = Number(data.row_number);
    if (!Number.isInteger(row) || row < 2) {
      throw new Error('Invalid row_number');
    }

    const bytes = Utilities.base64Decode(data.image_base64);
    if (bytes.length > MAX_IMAGE_BYTES) {
      throw new Error('Image exceeds the 2 MB limit');
    }

    const spreadsheet = SpreadsheetApp.openById(data.spreadsheet_id);
    const sheet = spreadsheet.getSheetByName(data.sheet_name);
    if (!sheet) {
      throw new Error('Worksheet not found');
    }

    const imageName = String(data.image_key || 'bug-photo');
    const blob = Utilities.newBlob(
      bytes,
      data.mime_type || 'image/jpeg',
      imageName + '.jpg',
    );
    const image = sheet.insertImage(blob, 2, row);
    image.setWidth(220);
    image.setHeight(140);
    image.setAltTextTitle(imageName);
    sheet.setRowHeight(row, 150);

    return jsonResponse({ok: true});
  } catch (error) {
    return jsonResponse({ok: false, error: String(error.message || error)});
  }
}

function jsonResponse(value) {
  return ContentService
    .createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}
