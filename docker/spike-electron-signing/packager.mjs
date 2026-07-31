// @electron/packager driver for the spike (issue #1197).
// Packages the pre-built lexed app (dist/ + dist-electron/) into a .app
// (darwin) or a linux app dir. Mirrors lexed's electron-builder `files`
// contract: the shipped app is dist + dist-electron + package.json only
// (main-process deps are bundled into main.cjs by vite-plugin-electron).
// Usage: node packager.mjs <darwin|linux> <appDir> <outDir>
import { packager } from '@electron/packager'
import fs from 'node:fs'
import path from 'node:path'

const [platform, appDir, outDir] = process.argv.slice(2)
if (!platform || !appDir || !outDir) {
  console.error('usage: node packager.mjs <darwin|linux> <appDir> <outDir>')
  process.exit(2)
}

const pkg = JSON.parse(fs.readFileSync(path.join(appDir, 'package.json'), 'utf-8'))

const KEEP = ['/dist', '/dist-electron', '/package.json']
const ignore = (p) => {
  if (p === '') return false // app root itself
  return !KEEP.some((k) => p === k || p.startsWith(`${k}/`))
}

// electron-builder equivalents: extendInfo (exported UTIs) + fileAssociations
// (CFBundleDocumentTypes). The exported UTIs are what the QuickLook appex's
// QLSupportedContentTypes bind against — without them the appex never fires.
const extendInfo = {
  UTExportedTypeDeclarations: [
    {
      UTTypeIdentifier: 'com.lex.document',
      UTTypeDescription: 'Lex Document',
      UTTypeConformsTo: ['public.plain-text'],
      UTTypeTagSpecification: { 'public.filename-extension': ['lex', 'lexd'] },
    },
    {
      UTTypeIdentifier: 'com.lex.extended-document',
      UTTypeDescription: 'Lex Extended Document',
      UTTypeConformsTo: ['public.plain-text'],
      UTTypeTagSpecification: { 'public.filename-extension': ['lexx'] },
    },
  ],
  CFBundleDocumentTypes: [
    {
      CFBundleTypeName: 'Lex Document',
      CFBundleTypeRole: 'Editor',
      LSHandlerRank: 'Owner',
      LSItemContentTypes: ['com.lex.document'],
      CFBundleTypeExtensions: ['lex', 'lexd'],
    },
    {
      CFBundleTypeName: 'Lex Extended Document',
      CFBundleTypeRole: 'Editor',
      LSHandlerRank: 'Owner',
      LSItemContentTypes: ['com.lex.extended-document'],
      CFBundleTypeExtensions: ['lexx'],
    },
  ],
}

const icns = path.join(appDir, 'build/icons/icons/icon.icns')

const paths = await packager({
  dir: appDir,
  out: outDir,
  name: platform === 'darwin' ? 'LexEd' : 'lexed',
  platform,
  arch: 'arm64',
  appBundleId: 'com.lex.lexed',
  appVersion: pkg.version,
  buildVersion: pkg.version,
  appCategoryType: 'public.app-category.productivity',
  icon: fs.existsSync(icns) ? icns : undefined,
  extendInfo: platform === 'darwin' ? extendInfo : undefined,
  prune: false, // nothing from node_modules ships (see KEEP above)
  ignore,
  overwrite: true,
})
console.log('packaged:', paths)
