// Metadata-clone GDAL dry run. No native pixels or regional destination exist.
#include "gdal_priv.h"
#include "gdalwarper.h"
#include "cpl_json.h"
#include "cpl_string.h"
#include <algorithm>
#include <chrono>
#include <cstring>
#include <memory>
#include <vector>
#include <limits>
#include <stdexcept>
struct State {
    std::vector<std::vector<int>> chunks;
    size_t read_calls=0, read_bytes=0;
    std::chrono::steady_clock::time_point start=std::chrono::steady_clock::now();
    bool timeout() const {return std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count()>60;}
};
class MetadataBand final: public GDALRasterBand {
    State* state; bool sink; bool has_nd; double nd; int flags;
    void fill(void* data,int w,int h,GDALDataType type,GSpacing pixel,GSpacing line) {
        if(!pixel)pixel=GDALGetDataTypeSizeBytes(type);if(!line)line=pixel*w;
        double value=has_nd?nd:0;
        for(int y=0;y<h;y++)GDALCopyWords(&value,GDT_Float64,0,(char*)data+y*line,type,(int)pixel,w);
    }
  public:
    MetadataBand(GDALDataset* ds,int width,int height,GDALDataType dtype,int bx,int by,bool has,double nodata,int mask_flags,bool is_sink,State* s):state(s),sink(is_sink),has_nd(has),nd(nodata),flags(mask_flags) {
        poDS=ds;nBand=1;nRasterXSize=width;nRasterYSize=height;eDataType=dtype;nBlockXSize=bx;nBlockYSize=by;eAccess=is_sink?GA_Update:GA_ReadOnly;
    }
    double GetNoDataValue(int* success=nullptr) override {if(success)*success=has_nd;return nd;}
    int GetMaskFlags() override {return flags;}
    CPLErr IReadBlock(int,int,void* data) override {fill(data,nBlockXSize,nBlockYSize,eDataType,0,0);state->read_calls++;return CE_None;}
    CPLErr IWriteBlock(int,int,void*) override {return CE_Failure;}
    CPLErr IRasterIO(GDALRWFlag rw,int x,int y,int w,int h,void* data,int bw,int bh,GDALDataType type,GSpacing pixel,GSpacing line,GDALRasterIOExtraArg*) override {
        if(state->timeout())return CE_Failure;
        if(rw==GF_Write){if(!sink||state->chunks.size()>=10000)return CE_Failure;state->chunks.push_back({x,y,w,h});return CE_None;}
        fill(data,bw,bh,type,pixel,line);state->read_calls++;state->read_bytes+=(size_t)bw*bh*GDALGetDataTypeSizeBytes(type);return CE_None;
    }
};
class MetadataDataset final: public GDALDataset {
    GDALGeoTransform gt; std::unique_ptr<OGRSpatialReference> srs;
  public:
    MetadataDataset(int w,int h,const GDALGeoTransform& transform,const OGRSpatialReference* crs,GDALDataType dtype,int bx,int by,bool has_nd,double nd,int flags,bool sink,State* state):gt(transform),srs(crs->Clone()) {
        nRasterXSize=w;nRasterYSize=h;eAccess=sink?GA_Update:GA_ReadOnly;
        SetBand(1,new MetadataBand(this,w,h,dtype,bx,by,has_nd,nd,flags,sink,state));
    }
    const OGRSpatialReference* GetSpatialRef() const override {return srs.get();}
    CPLErr GetGeoTransform(GDALGeoTransform& transform) const override {transform=gt;return CE_None;}
};
#ifndef VIEWSHED_PLANNER_SOURCE_SHA256
#define VIEWSHED_PLANNER_SOURCE_SHA256 ""
#endif
int main(int argc,char** argv) {
    try {
        GDALAllRegister();
        if(argc==2 && std::strcmp(argv[1],"--info")==0) {
            CPLJSONObject info;info.Add("gdal_version",GDALVersionInfo("RELEASE_NAME"));
            info.Add("source_code_sha256",VIEWSHED_PLANNER_SOURCE_SHA256);
            CPLJSONDocument document;document.SetRoot(info);fprintf(stdout,"%s\n",document.GetRoot().Format(CPLJSONObject::PrettyFormat::Plain).c_str());return 0;
        }
        if(argc!=3)throw std::runtime_error("usage: native_plan request.json output.json");GDALSetCacheMax64(64*1024*1024);
        CPLJSONDocument request;if(!request.Load(argv[1]))throw std::runtime_error("invalid request");auto root=request.GetRoot();
        auto original=(GDALDataset*)GDALOpen(root.GetString("source").c_str(),GA_ReadOnly);if(!original)throw std::runtime_error("source metadata unavailable");
        if(original->GetRasterCount()!=1||original->GetGCPCount()||original->GetMetadata("RPC")||original->GetMetadata("GEOLOCATION"))throw std::runtime_error("single affine raster only");
        auto band=original->GetRasterBand(1);
        if(band->GetMaskFlags()!=GMF_NODATA && band->GetMaskFlags()!=GMF_ALL_VALID)throw std::runtime_error("external/alpha masks require separate planner qualification");
        int has_nd=0,bx,by;double nd=band->GetNoDataValue(&has_nd);band->GetBlockSize(&bx,&by);GDALGeoTransform source_gt;if(original->GetGeoTransform(source_gt)!=CE_None||!original->GetSpatialRef())throw std::runtime_error("missing affine CRS");
        State state;MetadataDataset source(original->GetRasterXSize(),original->GetRasterYSize(),source_gt,original->GetSpatialRef(),band->GetRasterDataType(),bx,by,has_nd,nd,band->GetMaskFlags(),false,&state);
        GDALDataType source_dtype=band->GetRasterDataType();GDALClose(original);original=nullptr;
        int width=root.GetInteger("width"),height=root.GetInteger("height"),block=root.GetInteger("block_size");if(width<1||height<1||block<1)throw std::runtime_error("invalid grid");
        GDALGeoTransform target_gt;auto list=root.GetArray("transform");if(list.Size()!=6)throw std::runtime_error("six affine coefficients required");for(int i=0;i<6;i++)target_gt[i]=list[i].ToDouble();
        OGRSpatialReference target_srs;if(target_srs.SetFromUserInput(root.GetString("crs").c_str())!=OGRERR_NONE)throw std::runtime_error("invalid target CRS");target_srs.SetAxisMappingStrategy(OAMS_TRADITIONAL_GIS_ORDER);
        bool canopy=root.GetString("resampling")=="max";if(!canopy&&root.GetString("resampling")!="bilinear")throw std::runtime_error("unsupported method");
        double dst_nd=canopy?std::numeric_limits<double>::quiet_NaN():nd;bool has_dst_nd=canopy||has_nd;
        MetadataDataset sink(width,height,target_gt,&target_srs,canopy?GDT_Float32:source_dtype,block,block,has_dst_nd,dst_nd,has_dst_nd?GMF_NODATA:GMF_ALL_VALID,true,&state);
        GDALWarpOptions* options=GDALCreateWarpOptions();options->hSrcDS=&source;options->hDstDS=&sink;options->eResampleAlg=canopy?GRA_Max:GRA_Bilinear;GDALWarpInitDefaultBandMapping(options,1);
        if(has_nd)GDALWarpInitSrcNoDataReal(options,nd);if(has_dst_nd)GDALWarpInitDstNoDataReal(options,dst_nd);
        options->papszWarpOptions=CSLSetNameValue(options->papszWarpOptions,"UNIFIED_SRC_NODATA","YES");options->papszWarpOptions=CSLSetNameValue(options->papszWarpOptions,"NUM_THREADS","1");options->papszWarpOptions=CSLSetNameValue(options->papszWarpOptions,"INIT_DEST",has_dst_nd?"NO_DATA":"0");
        void* transform=GDALCreateGenImgProjTransformer2(&source,&sink,nullptr);if(!transform)throw std::runtime_error("transformer unavailable");void* approx=GDALCreateApproxTransformer(GDALGenImgProjTransform,transform,0.125);GDALApproxTransformerOwnsSubtransformer(approx,TRUE);options->pfnTransformer=GDALApproxTransform;options->pTransformerArg=approx;
        auto operation=GDALCreateWarpOperation(options);if(!operation)throw std::runtime_error("warp planner initialization failed");
        auto error=GDALChunkAndWarpImage(operation,0,0,width,height);GDALDestroyWarpOperation(operation);GDALDestroyWarpOptions(options);GDALDestroyApproxTransformer(approx);if(error!=CE_None)throw std::runtime_error("metadata clone dry run failed/timeout");
        CPLJSONObject result;result.Add("gdal_version",GDALVersionInfo("RELEASE_NAME"));result.Add("source_code_sha256",VIEWSHED_PLANNER_SOURCE_SHA256);result.Add("native_raster_pixel_reads",0);result.Add("regional_rasters_materialized",0);result.Add("dummy_read_calls",(GInt64)state.read_calls);result.Add("dummy_read_bytes",(GInt64)state.read_bytes);CPLJSONArray chunks;for(auto& chunk:state.chunks){CPLJSONArray item;for(int value:chunk)item.Add(value);chunks.Add(item);}result.Add("chunks",chunks);CPLJSONDocument output;output.SetRoot(result);if(!output.Save(argv[2]))throw std::runtime_error("cannot save plan");return 0;
    }catch(const std::exception& ex){fprintf(stderr,"%s\n",ex.what());return 2;}
}
