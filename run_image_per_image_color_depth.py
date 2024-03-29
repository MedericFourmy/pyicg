import cv2
import glob
import time
import yaml
import argparse
import numpy as np
import quaternion
from pathlib import Path

import pyicg

def inv_SE3(T):
    """
    Inverse of an SE(3) 4x4 array
    """
    Tinv = np.eye(4)
    Tinv[:3,:3] = T[:3,:3].T
    Tinv[:3,3] = -T[:3,:3].T@T[:3,3]
    return Tinv


def tq_to_SE3(t, q):
    """
    t: translation as list or array
    q: quaternion as list or array, expected order: xyzw
    out: 4x4 array representing the SE(3) transformation
    """
    T = np.eye(4)
    T[:3,3] = t
    # np.quaternion constructor uses wxyz order convention
    quat = np.quaternion(q[3], q[0], q[1], q[2]).normalized()
    T[:3,:3] = quaternion.as_rotation_matrix(quat)
    return T


def parse_script_input():
    parser = argparse.ArgumentParser(
        prog='run_image_per_image',
        description='Run the icg tracker image per image on '
    )

    parser.add_argument('-b', '--body_name',  dest='body_name',  type=str, required=True, help='Name of the object to track. need to match')
    parser.add_argument('-m', '--models_dir', dest='models_dir', type=str, required=True, help='Path to directory where object model file .obj is stored')
    parser.add_argument('-i', '--imgs_dir',   dest='imgs_dir',   type=str, required=True, help='Path to directory where "rbg*" and "depth*" named images are stored')
    parser.add_argument('--config_dir', dest='config_dir', type=str, default='config', help='Path to directory where <body_name>.yaml and static_detector.yaml files are stored')
    parser.add_argument('--tmp_dir',    dest='tmp_dir',    type=str, default='tmp', help='Directory to store preprocessing files generated by the tracker.')
    parser.add_argument('--detector_file', dest='detector_file', type=str, default='static_detector.yaml')
    parser.add_argument('--camera_file',   dest='camera_file',   type=str, default='cam_d435_640.yaml')
    parser.add_argument('--nb_img_load',   dest='nb_img_load',   type=int, default=-1)
    parser.add_argument('--use_depth',     dest='use_depth',     action='store_true', default=False)
    parser.add_argument('--model_occlusions', dest='model_occlusions', action='store_true', default=False)
    parser.add_argument('-s', '--stop',    dest='stop',          action='store_true', default=False)

    return parser.parse_args()


args = parse_script_input()

body_name = args.body_name
models_dir = Path(args.models_dir)
imgs_dir = Path(args.imgs_dir)
config_dir = Path(args.config_dir)
tmp_dir = Path(args.tmp_dir)
detector_file = args.detector_file
camera_file = args.camera_file
use_depth = args.use_depth
model_occlusions = args.model_occlusions
nb_img_load = args.nb_img_load
stop = args.stop


tracker = pyicg.Tracker('tracker', synchronize_cameras=False)

renderer_geometry = pyicg.RendererGeometry('renderer geometry')

with open(config_dir / camera_file, 'r') as f:
    cam = yaml.load(f.read(), Loader=yaml.UnsafeLoader)

color_camera = pyicg.DummyColorCamera('cam_color')
color_camera.color2depth_pose = tq_to_SE3(cam['trans_d_c'], cam['quat_d_c_xyzw'])
color_camera.intrinsics = pyicg.Intrinsics(**cam['intrinsics_color'])

depth_camera = pyicg.DummyDepthCamera('cam_depth')
depth_camera.depth2color_pose = inv_SE3(color_camera.color2depth_pose)
depth_camera.intrinsics = pyicg.Intrinsics(**cam['intrinsics_depth'])

# Viewers
color_viewer = pyicg.NormalColorViewer('color_viewer', color_camera, renderer_geometry)
# color_viewer.StartSavingImages('tmp', 'bmp')
color_viewer.set_opacity(0.5)  # [0.0-1.0]
depth_viewer = pyicg.NormalDepthViewer('depth_viewer', depth_camera, renderer_geometry)
tracker.AddViewer(depth_viewer)
tracker.AddViewer(color_viewer)


# Bodies
metafile_path = models_dir / (body_name+'.yaml')
body = pyicg.Body(body_name, metafile_path.as_posix())
renderer_geometry.AddBody(body)

# Detector
detector_path = config_dir / detector_file
detector = pyicg.StaticDetector('static_detector', detector_path.as_posix(), body)
tracker.AddDetector(detector)

# Models
region_model_path = tmp_dir / (body_name + '_region_model.bin')
region_model = pyicg.RegionModel(body_name + '_region_model', body, region_model_path.as_posix())
depth_model_path = tmp_dir / (body_name + '_depth_model.bin')
depth_model = pyicg.DepthModel(body_name + '_depth_model', body, depth_model_path.as_posix())

# Modalities
region_modality = pyicg.RegionModality(body_name + '_region_modality', body, color_camera, region_model)
depth_modality = pyicg.DepthModality(body_name + '_depth_modality', body, depth_camera, depth_model)




if model_occlusions:
    raise NotImplementedError('region_modality.ModelOcclusions binding does not work properly yet')

    """
    FocusedRenderer: interface with OpenGL, render only part of the image where tracked objects are present 
                    -> projection matrix is recomputed each time a new render is done (contrary to FullRender)
    Used for render based occlusion handling.
    """
    # We need 2 renderers because depth and color are slightly not aligned
    color_depth_renderer = pyicg.FocusedBasicDepthRenderer('color_depth_renderer', renderer_geometry, color_camera)
    depth_depth_renderer = pyicg.FocusedBasicDepthRenderer('depth_depth_renderer', renderer_geometry, depth_camera)
    color_depth_renderer.AddReferencedBody(body)
    depth_depth_renderer.AddReferencedBody(body)

    region_modality.ModelOcclusions(color_depth_renderer)
    depth_modality.ModelOcclusions(depth_depth_renderer)


optimizer = pyicg.Optimizer(body_name+'_optimizer')
optimizer.AddModality(region_modality)
if use_depth:
    optimizer.AddModality(depth_modality)
tracker.AddOptimizer(optimizer)

# Do all the necessary heavy preprocessing
ok = tracker.SetUp()
print('tracker.SetUp ok: ', ok)

# read images from disk
rgb_names = sorted(glob.glob((imgs_dir / 'bgr*').as_posix()))
depth_names = sorted(glob.glob((imgs_dir / 'depth*').as_posix()))

# LIMIT nb images
rgb_names = rgb_names[:nb_img_load]
depth_names = depth_names[:nb_img_load]
print(f'{len(rgb_names)} images to load')

# load images from disk
color_read_flags = cv2.IMREAD_COLOR + cv2.IMREAD_ANYDEPTH
img_bgr_lst = [cv2.imread(name, color_read_flags) for name in rgb_names]  # loads a dtype=uint8 array
depth_read_flags = cv2.IMREAD_GRAYSCALE + cv2.IMREAD_ANYDEPTH
img_depth_lst = [cv2.imread(name, depth_read_flags) for name in depth_names]  # loads a dtype=uint8 array

print(tracker.n_corr_iterations)
print(tracker.n_update_iterations)
# tracker.n_update_iterations = 2
tracker.n_update_iterations = 5

# Simulate one iteration of Tracker::RunTrackerProcess for loop
for iter, (img_bgr, img_depth) in enumerate(zip(img_bgr_lst, img_depth_lst)):
    print('Iter: ', iter)
    # 1) Update camera image -> replaces a call to the camera UpdateImage method (which does nothing for Dummy(Color|Depth)Camera) 
    color_camera.image = img_bgr
    depth_camera.image = img_depth
    ok = tracker.UpdateCameras(True)  # poststep verifying the images have been properly setup
    if not ok:
        raise ValueError('Something is wrong with the provided images')

    if iter == 0:
        # 2) Use detector or external init to update initial object pose
        body.body2world_pose = detector.body2world_pose  # simulate external initial pose
        # tracker.ExecuteDetectionCycle(iter)  # use detectector
        # tracker.DetectBodies()  # use detectector

        # 3) Initialise the different modalities (e.g. the color histograms)
        tracker.StartModalities(iter)
        
    # 4) One tracking cycle (could call it several time)
    t = time.time()
    tracker.ExecuteTrackingCycle(iter)
    print('ExecuteTrackingCycle (ms)', 1000*(time.time() - t))

    # 5) Render results
    t = time.time()
    tracker.UpdateViewers(iter)
    print('UpdateViewers (ms)', 1000*(time.time() - t))

    if stop:
        cv2.waitKey(0)

