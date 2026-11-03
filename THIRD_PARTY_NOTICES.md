# Third-party notices

## Weather data

Weather data: Open-Meteo, https://open-meteo.com/.
Licence: Creative Commons Attribution 4.0 International,
https://creativecommons.org/licenses/by/4.0/.
Terms: https://open-meteo.com/en/terms.

Salang maps weather codes to Korean descriptions and decorative animations.
Its seasonal tree, falling leaves, and snow accumulation are decorative
interpretations and do not represent measured vegetation or snow depth.
The public weather API is for non-commercial use under the provider's terms.

## Korean region catalog

Provider: Korea Meteorological Administration (기상청).
Source: https://www.data.go.kr/data/15084084/openapi.do.
Attachment version: 2610; sheet: 최종 업데이트 파일_20260930.
Licence: Korea Open Government License Type 1 (attribution),
https://www.kogl.or.kr/info/licenseType1.do.

Selected columns were converted to an offline JSON catalog. Two special
Ieodo records whose original latitude and longitude were both zero were
excluded. Original coordinate values were retained. See korea-regions.json
for the source workbook SHA-256 and excluded record codes.

## Standalone executables

Standalone releases include a CPython interpreter under the Python Software
Foundation licence and the required standard-library components. Their
licence texts are included in the executable and displayed by salang --licenses.

Build tool: PyInstaller 6.22.3, GPL-2.0-or-later with the PyInstaller exception.
https://pyinstaller.org/en/stable/license.html
https://github.com/pyinstaller/pyinstaller

Build dependencies are pinned with SHA-256 hashes in requirements-build.txt.
Their use does not change the individual licences of the data or software.
