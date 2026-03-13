using Autodesk.AutoCAD.Runtime;
using Autodesk.AutoCAD.ApplicationServices.Core;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using System;
using System.Collections.Generic;
using System.IO;

[assembly: CommandClass(typeof(StlExporter.Commands))]
[assembly: ExtensionApplication(null)]

namespace StlExporter
{
    public class Commands
    {
        const double TubeRadius = 0.005;
        const int TubeSegments = 6;
        const int CurveSegments = 32;
        const int MaxBlockDepth = 20;

        static string _outputDir;
        static Autodesk.AutoCAD.EditorInput.Editor _ed;
        static int _solidCount;
        static List<string> _tempFiles;
        static List<byte[]> _manualTriangles;
        static Dictionary<string, int> _entityCounts;
        static Vector3d _offset;
        static bool _needsTranslation;

        [CommandMethod("ExportToStl", CommandFlags.Modal)]
        public static void ExportToStl()
        {
            var doc = Application.DocumentManager.MdiActiveDocument;
            _ed = doc.Editor;

            try
            {
                var db = doc.Database;
                _outputDir = Path.GetDirectoryName(doc.Name);
                if (string.IsNullOrEmpty(_outputDir))
                    _outputDir = Environment.CurrentDirectory;

                string outputFile = Path.Combine(_outputDir, "result.stl");
                _tempFiles = new List<string>();
                _manualTriangles = new List<byte[]>();
                _solidCount = 0;
                _entityCounts = new Dictionary<string, int>();

                _ed.WriteMessage("\n[StlExporter] starting...");
                _ed.WriteMessage("\n[StlExporter] output dir: " + _outputDir);

                using (var tr = db.TransactionManager.StartTransaction())
                {
                    var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
                    var ms = (BlockTableRecord)tr.GetObject(
                        bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);

                    // Phase 1: compute bounding box across all entities (including block contents)
                    Point3d globalMin = new Point3d(double.MaxValue, double.MaxValue, double.MaxValue);
                    foreach (ObjectId id in ms)
                    {
                        var ent = tr.GetObject(id, OpenMode.ForRead) as Entity;
                        if (ent == null) continue;
                        try
                        {
                            var ext = ent.GeometricExtents;
                            globalMin = new Point3d(
                                Math.Min(globalMin.X, ext.MinPoint.X),
                                Math.Min(globalMin.Y, ext.MinPoint.Y),
                                Math.Min(globalMin.Z, ext.MinPoint.Z));
                        }
                        catch { }
                    }

                    double offsetX = globalMin.X < 0 ? -globalMin.X + 1.0 : 0;
                    double offsetY = globalMin.Y < 0 ? -globalMin.Y + 1.0 : 0;
                    double offsetZ = globalMin.Z < 0 ? -globalMin.Z + 1.0 : 0;
                    _needsTranslation = offsetX > 0 || offsetY > 0 || offsetZ > 0;
                    _offset = new Vector3d(offsetX, offsetY, offsetZ);

                    if (_needsTranslation)
                        _ed.WriteMessage("\n[StlExporter] translating by (" + offsetX + ", " + offsetY + ", " + offsetZ + ")");

                    // Phase 2: process all entities recursively
                    foreach (ObjectId id in ms)
                    {
                        var ent = tr.GetObject(id, OpenMode.ForRead) as Entity;
                        if (ent == null) continue;
                        ProcessEntity(ent, tr, db, true);
                    }

                    tr.Commit();
                }

                // Phase 3: write manual triangles (faces, curves) to STL
                if (_manualTriangles.Count > 0)
                {
                    string triFile = Path.Combine(_outputDir, "temp_triangles.stl");
                    WriteBinaryStl(triFile, _manualTriangles);
                    _tempFiles.Add(triFile);
                    _ed.WriteMessage("\n[StlExporter] wrote " + _manualTriangles.Count + " manual triangles");
                }

                // Phase 4: report
                _ed.WriteMessage("\n[StlExporter] entity counts:");
                foreach (var kvp in _entityCounts)
                    _ed.WriteMessage("\n  " + kvp.Key + ": " + kvp.Value);
                _ed.WriteMessage("\n[StlExporter] exported " + _tempFiles.Count + " STL chunks (" + _solidCount + " solids)");

                // Phase 5: merge all STL files
                if (_tempFiles.Count == 0)
                {
                    _ed.WriteMessage("\n[StlExporter] no geometry exported, creating empty marker");
                    File.WriteAllText(outputFile, "NO_GEOMETRY_EXPORTED");
                    return;
                }

                if (_tempFiles.Count == 1)
                {
                    if (File.Exists(outputFile)) File.Delete(outputFile);
                    File.Move(_tempFiles[0], outputFile);
                }
                else
                {
                    MergeBinaryStl(_tempFiles, outputFile);
                    foreach (var f in _tempFiles)
                    {
                        try { File.Delete(f); } catch { }
                    }
                }

                var info = new FileInfo(outputFile);
                _ed.WriteMessage("\n[StlExporter] done: " + outputFile + " (" + info.Length + " bytes)");
            }
            catch (System.Exception ex)
            {
                _ed.WriteMessage("\n[StlExporter] FATAL ERROR: " + ex.ToString());
            }
        }

        static void RecordType(string type)
        {
            if (!_entityCounts.ContainsKey(type))
                _entityCounts[type] = 0;
            _entityCounts[type]++;
        }

        static void ProcessEntity(Entity ent, Transaction tr, Database db, bool isDbResident)
        {
            if (ent is BlockReference bref)
            {
                RecordType("INSERT");
                ProcessBlockReference(bref, tr, db);
            }
            else if (ent is Solid3d solid)
            {
                RecordType("3DSOLID");
                ExportSolid(solid, tr, isDbResident);
            }
            else if (ent is Face face)
            {
                RecordType("3DFACE");
                ExportFace(face);
            }
            else if (ent is SubDMesh mesh)
            {
                RecordType("MESH");
                ExportSubDMesh(mesh);
            }
            else if (ent is Curve curve)
            {
                string typeName = ent.GetRXClass().DxfName ?? ent.GetType().Name;
                RecordType(typeName);
                ExportCurve(curve);
            }
        }

        // --- BlockReference: explode recursively ---

        static void ProcessBlockReference(BlockReference bref, Transaction tr, Database db)
        {
            try
            {
                var exploded = new DBObjectCollection();
                bref.Explode(exploded);
                foreach (DBObject obj in exploded)
                {
                    if (obj is Entity subEnt)
                    {
                        ProcessEntity(subEnt, tr, db, false);
                        subEnt.Dispose();
                    }
                    else
                    {
                        obj.Dispose();
                    }
                }
            }
            catch (System.Exception ex)
            {
                _ed.WriteMessage("\n[StlExporter] block explode error: " + ex.Message);
            }
        }

        // --- Solid3d: export via StlOut ---

        static void ExportSolid(Solid3d solid, Transaction tr, bool isDbResident)
        {
            _solidCount++;
            string tempFile = Path.Combine(_outputDir, "temp_" + _solidCount + ".stl");

            try
            {
                if (isDbResident)
                {
                    var dbSolid = (Solid3d)tr.GetObject(solid.ObjectId, OpenMode.ForWrite);
                    if (_needsTranslation)
                        dbSolid.TransformBy(Matrix3d.Displacement(_offset));
                    dbSolid.StlOut(tempFile, false);
                    if (_needsTranslation)
                        dbSolid.TransformBy(Matrix3d.Displacement(-_offset));
                }
                else
                {
                    if (_needsTranslation)
                        solid.TransformBy(Matrix3d.Displacement(_offset));
                    solid.StlOut(tempFile, false);
                }

                if (File.Exists(tempFile) && new FileInfo(tempFile).Length > 84)
                {
                    _tempFiles.Add(tempFile);
                }
                else
                {
                    _ed.WriteMessage("\n[StlExporter] solid " + _solidCount + " -> empty STL");
                    if (File.Exists(tempFile)) File.Delete(tempFile);
                }
            }
            catch (System.Exception ex)
            {
                _ed.WriteMessage("\n[StlExporter] solid " + _solidCount + " ERROR: " + ex.Message);
                if (File.Exists(tempFile)) try { File.Delete(tempFile); } catch { }
            }
        }

        // --- 3DFace: extract triangle vertices directly ---

        static void ExportFace(Face face)
        {
            try
            {
                var v0 = face.GetVertexAt(0);
                var v1 = face.GetVertexAt(1);
                var v2 = face.GetVertexAt(2);
                var v3 = face.GetVertexAt(3);

                if (_needsTranslation)
                {
                    v0 += _offset;
                    v1 += _offset;
                    v2 += _offset;
                    v3 += _offset;
                }

                _manualTriangles.Add(MakeTriangleBytes(v0, v1, v2));
                if (!v2.IsEqualTo(v3))
                    _manualTriangles.Add(MakeTriangleBytes(v0, v2, v3));
            }
            catch (System.Exception ex)
            {
                _ed.WriteMessage("\n[StlExporter] face error: " + ex.Message);
            }
        }

        // --- SubDMesh: extract vertex/face arrays ---

        static void ExportSubDMesh(SubDMesh mesh)
        {
            try
            {
                var verts = mesh.Vertices;
                var faceArray = mesh.FaceArray;

                var pts = new Point3d[verts.Count];
                for (int i = 0; i < verts.Count; i++)
                {
                    pts[i] = verts[i];
                    if (_needsTranslation)
                        pts[i] += _offset;
                }

                int pos = 0;
                while (pos < faceArray.Count)
                {
                    int count = faceArray[pos++];
                    if (count < 3 || pos + count > faceArray.Count) break;

                    int[] idx = new int[count];
                    for (int i = 0; i < count; i++)
                        idx[i] = faceArray[pos++];

                    for (int i = 1; i < count - 1; i++)
                    {
                        _manualTriangles.Add(MakeTriangleBytes(
                            pts[idx[0]], pts[idx[i]], pts[idx[i + 1]]));
                    }
                }
            }
            catch (System.Exception ex)
            {
                _ed.WriteMessage("\n[StlExporter] mesh error: " + ex.Message);
            }
        }

        // --- Curves: sample points along the curve, generate tube geometry ---

        static void ExportCurve(Curve curve)
        {
            try
            {
                var points = SampleCurve(curve);
                if (points.Count < 2) return;

                if (_needsTranslation)
                {
                    for (int i = 0; i < points.Count; i++)
                        points[i] += _offset;
                }

                for (int i = 0; i < points.Count - 1; i++)
                    AddTubeSegment(points[i], points[i + 1]);
            }
            catch (System.Exception ex)
            {
                _ed.WriteMessage("\n[StlExporter] curve error: " + ex.Message);
            }
        }

        static List<Point3d> SampleCurve(Curve curve)
        {
            var points = new List<Point3d>();
            double start = curve.StartParam;
            double end = curve.EndParam;

            int segments;
            if (curve is Line)
                segments = 1;
            else
                segments = CurveSegments;

            for (int i = 0; i <= segments; i++)
            {
                double t = start + (end - start) * i / segments;
                points.Add(curve.GetPointAtParameter(t));
            }

            if (curve.Closed && points.Count > 1)
                points.Add(points[0]);

            return points;
        }

        // --- Tube geometry: create a tube mesh between two points ---

        static void AddTubeSegment(Point3d p1, Point3d p2)
        {
            var dir = p2 - p1;
            if (dir.Length < 1e-10) return;
            dir = dir.GetNormal();

            Vector3d perp1;
            if (Math.Abs(dir.DotProduct(Vector3d.YAxis)) < 0.99)
                perp1 = dir.CrossProduct(Vector3d.YAxis).GetNormal();
            else
                perp1 = dir.CrossProduct(Vector3d.XAxis).GetNormal();
            var perp2 = dir.CrossProduct(perp1).GetNormal();

            var ring1 = new Point3d[TubeSegments];
            var ring2 = new Point3d[TubeSegments];
            for (int i = 0; i < TubeSegments; i++)
            {
                double angle = 2 * Math.PI * i / TubeSegments;
                var radial = TubeRadius * (Math.Cos(angle) * perp1 + Math.Sin(angle) * perp2);
                ring1[i] = p1 + radial;
                ring2[i] = p2 + radial;
            }

            for (int i = 0; i < TubeSegments; i++)
            {
                int next = (i + 1) % TubeSegments;
                _manualTriangles.Add(MakeTriangleBytes(ring1[i], ring2[i], ring2[next]));
                _manualTriangles.Add(MakeTriangleBytes(ring1[i], ring2[next], ring1[next]));
            }
        }

        // --- Binary STL helpers ---

        static byte[] MakeTriangleBytes(Point3d v1, Point3d v2, Point3d v3)
        {
            var edge1 = v2 - v1;
            var edge2 = v3 - v1;
            var normal = edge1.CrossProduct(edge2);
            if (normal.Length > 1e-10)
                normal = normal.GetNormal();

            byte[] data = new byte[50];
            int pos = 0;
            WriteFloat(data, ref pos, (float)normal.X);
            WriteFloat(data, ref pos, (float)normal.Y);
            WriteFloat(data, ref pos, (float)normal.Z);
            WriteFloat(data, ref pos, (float)v1.X);
            WriteFloat(data, ref pos, (float)v1.Y);
            WriteFloat(data, ref pos, (float)v1.Z);
            WriteFloat(data, ref pos, (float)v2.X);
            WriteFloat(data, ref pos, (float)v2.Y);
            WriteFloat(data, ref pos, (float)v2.Z);
            WriteFloat(data, ref pos, (float)v3.X);
            WriteFloat(data, ref pos, (float)v3.Y);
            WriteFloat(data, ref pos, (float)v3.Z);
            return data;
        }

        static void WriteFloat(byte[] data, ref int pos, float value)
        {
            var bytes = BitConverter.GetBytes(value);
            Array.Copy(bytes, 0, data, pos, 4);
            pos += 4;
        }

        static void WriteBinaryStl(string filename, List<byte[]> triangles)
        {
            using (var fs = new FileStream(filename, FileMode.Create))
            using (var bw = new BinaryWriter(fs))
            {
                byte[] header = new byte[80];
                System.Text.Encoding.ASCII.GetBytes("Binary STL - StlExporter")
                    .CopyTo(header, 0);
                bw.Write(header);
                bw.Write((uint)triangles.Count);
                foreach (var tri in triangles)
                    bw.Write(tri);
            }
        }

        static void MergeBinaryStl(List<string> inputFiles, string outputFile)
        {
            uint totalTriangles = 0;
            var allTriangleData = new List<byte[]>();

            foreach (var file in inputFiles)
            {
                byte[] data = File.ReadAllBytes(file);
                if (data.Length < 84) continue;

                uint count = BitConverter.ToUInt32(data, 80);
                totalTriangles += count;

                byte[] triangles = new byte[data.Length - 84];
                Array.Copy(data, 84, triangles, 0, triangles.Length);
                allTriangleData.Add(triangles);
            }

            using (var fs = new FileStream(outputFile, FileMode.Create))
            using (var bw = new BinaryWriter(fs))
            {
                byte[] header = new byte[80];
                System.Text.Encoding.ASCII.GetBytes("Binary STL merged by StlExporter")
                    .CopyTo(header, 0);
                bw.Write(header);
                bw.Write(totalTriangles);
                foreach (var data in allTriangleData)
                    bw.Write(data);
            }
        }
    }
}
