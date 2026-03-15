-- DWG to FBX converter for Design Automation (3ds Max engine)
-- Input DWG is placed at working directory as "input.dwg" by DA
-- Output FBX is written as "result.fbx" for DA to upload

inputPath = sysInfo.currentdir + "\\input.dwg"
outputPath = sysInfo.currentdir + "\\result.fbx"
logPath = sysInfo.currentdir + "\\log.txt"

logFile = createFile logPath

format "Importing DWG: %\n" inputPath to:logFile

try (
    setINISetting (getDir #plugcfg + "\\DWGImpExp.ini") "General" "DeriveBy" "2"
    setINISetting (getDir #plugcfg + "\\DWGImpExp.ini") "General" "AssignMaterials" "1"
    format "Set DWG import preferences\n" to:logFile
) catch (
    format "Could not set DWG import preferences: %\n" (getCurrentException()) to:logFile
)

importFile inputPath #noprompt

objCount = objects.count
format "Imported % objects\n" objCount to:logFile

if objCount == 0 then (
    format "ERROR: No geometry imported from DWG\n" to:logFile
    close logFile
    quitMax #noPrompt
)

-- Remove line-like objects (2D lines/arcs: two dimensions near-zero, only length)
toDelete = #()
for obj in objects where (superClassOf obj == GeometryClass) do (
    try (
        local m = snapshotAsMesh obj
        local nf = m.numFaces
        local bmin = obj.min
        local bmax = obj.max
        local dx = abs(bmax.x - bmin.x)
        local dy = abs(bmax.y - bmin.y)
        local dz = abs(bmax.z - bmin.z)
        local dims = #(dx, dy, dz)
        sort dims
        -- A line has two tiny dimensions and one long one
        -- A plate has one tiny dimension and two large ones
        -- Only remove lines: smallest TWO dims < 0.05 and few faces
        if dims[1] < 0.05 and dims[2] < 0.05 and nf <= 8 then (
            format "  REMOVE line: obj=% layer=% faces=% dims=(% % %)\n" obj.name obj.layer.name nf dims[1] dims[2] dims[3] to:logFile
            append toDelete obj
        )
        delete m
    ) catch ()
)
format "Removing % line objects\n" toDelete.count to:logFile
for obj in toDelete do delete obj

-- Log remaining objects
format "--- Remaining objects: % ---\n" objects.count to:logFile
uniqueColors = #()
for obj in objects do (
    wc = obj.wirecolor
    ln = obj.layer.name
    format "  obj=% layer=% wirecolor=(% % %)\n" obj.name ln wc.r wc.g wc.b to:logFile
    colorKey = (wc as string)
    if (findItem uniqueColors colorKey) == 0 then append uniqueColors colorKey
)
format "Unique wirecolors: %\n" uniqueColors.count to:logFile

-- Assign materials from wirecolor
matAssigned = 0
matCache = #()
matCacheColors = #()
for obj in objects do (
    wc = obj.wirecolor
    colorKey = (wc as string)
    idx = findItem matCacheColors colorKey
    if idx > 0 then (
        obj.material = matCache[idx]
    ) else (
        m = standardMaterial()
        m.name = "mat_" + colorKey
        m.diffuse = wc
        m.ambient = wc
        obj.material = m
        append matCache m
        append matCacheColors colorKey
    )
    matAssigned += 1
)
format "Assigned % materials (% unique)\n" matAssigned matCache.count to:logFile

pluginManager.loadClass FbxExporter
FBXExporterSetParam "Animation" false
FBXExporterSetParam "Cameras" false
FBXExporterSetParam "Lights" false
FBXExporterSetParam "SmoothingGroups" true
FBXExporterSetParam "ASCII" false
FBXExporterSetParam "EmbedTextures" false

exportFile outputPath #noprompt using:FBXEXP

format "Exported FBX: %\n" outputPath to:logFile
format "Done.\n" to:logFile
close logFile
