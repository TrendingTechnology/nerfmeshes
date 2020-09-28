import argparse
import numpy as np
import torch
import torchvision
import yaml

from nerf.train_utils import predict_and_render_radiance
from skimage import measure
from scipy.spatial import KDTree
from tqdm import tqdm

from nerf import get_minibatches

from nerf.nerf_helpers import cumprod_exclusive
from nerf import (
    CfgNode,
    get_ray_bundle,
    load_blender_data,
    load_llff_data,
    models,
    get_embedding_function,
    run_one_iter_of_nerf,
)


def cast_to_image(tensor, dataset_type):
    # Input tensor is (H, W, 3). Convert to (3, H, W).
    tensor = tensor.permute(2, 0, 1)
    # Convert to PIL Image and then np.array (output shape: (H, W, 3))
    img = np.array(torchvision.transforms.ToPILImage()(tensor.detach().cpu()))
    return img
    # # Map back to shape (3, H, W), as tensorboard needs channels first.
    # return np.moveaxis(img, [-1], [0])


def cast_to_disparity_image(tensor):
    img = (tensor - tensor.min()) / (tensor.max() - tensor.min())
    img = img.clamp(0, 1) * 255
    return img.detach().cpu().numpy().astype(np.uint8)


def export_obj(vertices, triangles, diffuse, normals, filename):
    """
    Exports a mesh in the (.obj) format.
    """
    print('Writing to obj')

    with open(filename, 'w') as fh:

        for index, v in enumerate(vertices):
            fh.write("v {} {} {} {} {} {}\n".format(*v, *diffuse[index]))

        for n in normals:
            fh.write("vn {} {} {}\n".format(*n))

        for f in triangles:
            fh.write("f")
            for index in f:
                fh.write(" {}//{}".format(index + 1, index + 1))

            fh.write("\n")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config", type = str, required = True, help = "Path to (.yml) config file."
    )
    parser.add_argument(
        "--base-dir",
        type = str,
        required = False,
        help = "Override the default base dir.",
    )
    parser.add_argument(
        "--checkpoint",
        type = str,
        required = True,
        help = "Checkpoint / pre-trained model to evaluate.",
    )
    parser.add_argument(
        "--save-dir", type = str, help = "Save mesh to this directory, if specified."
    )
    
    parser.add_argument(
        "--iso-level",
        type = float,
        help = "Iso-Level to be queried",
        default = 32
    )

    parser.add_argument('--cache-mesh', dest = 'cache_mesh', action = 'store_true')
    parser.add_argument('--no-cache-mesh', dest = 'cache_mesh', action = 'store_false')
    parser.set_defaults(cache_mesh = True)

    config_args = parser.parse_args()

    # Read config file.
    cfg = None
    with open(config_args.config, "r") as f:
        cfg_dict = yaml.load(f, Loader = yaml.FullLoader)
        cfg = CfgNode(cfg_dict)

    # Device on which to run.
    device = "cpu"
    if torch.cuda.is_available():
        device = "cuda"

    encode_position_fn = get_embedding_function(
        num_encoding_functions = cfg.models.coarse.num_encoding_fn_xyz,
        include_input = cfg.models.coarse.include_input_xyz,
        log_sampling = cfg.models.coarse.log_sampling_xyz,
    )

    encode_direction_fn = None
    if cfg.models.coarse.use_viewdirs:
        encode_direction_fn = get_embedding_function(
            num_encoding_functions = cfg.models.coarse.num_encoding_fn_dir,
            include_input = cfg.models.coarse.include_input_dir,
            log_sampling = cfg.models.coarse.log_sampling_dir,
        )

    # Initialize a coarse resolution model.
    model_coarse = getattr(models, cfg.models.coarse.type)(
        num_encoding_fn_xyz = cfg.models.coarse.num_encoding_fn_xyz,
        num_encoding_fn_dir = cfg.models.coarse.num_encoding_fn_dir,
        include_input_xyz = cfg.models.coarse.include_input_xyz,
        include_input_dir = cfg.models.coarse.include_input_dir,
        use_viewdirs = cfg.models.coarse.use_viewdirs,
    )
    model_coarse.to(device)

    # If a fine-resolution model is specified, initialize it.
    model_fine = None
    if hasattr(cfg.models, "fine"):
        model_fine = getattr(models, cfg.models.fine.type)(
            num_encoding_fn_xyz = cfg.models.fine.num_encoding_fn_xyz,
            num_encoding_fn_dir = cfg.models.fine.num_encoding_fn_dir,
            include_input_xyz = cfg.models.fine.include_input_xyz,
            include_input_dir = cfg.models.fine.include_input_dir,
            use_viewdirs = cfg.models.fine.use_viewdirs,
        )
        model_fine.to(device)

    checkpoint = torch.load(config_args.checkpoint)
    model_coarse.load_state_dict(checkpoint["model_coarse_state_dict"])
    if checkpoint["model_fine_state_dict"]:
        try:
            model_fine.load_state_dict(checkpoint["model_fine_state_dict"])
        except:
            print(
                "The checkpoint has a fine-level model, but it could "
                "not be loaded (possibly due to a mismatched config file."
            )

    model_coarse.eval()
    if model_fine:
        model_fine.eval()

    # Mesh Extraction
    N = 400
    iso_value = config_args.iso_level
    batch_size = 4096
    density_samples_count = 6
    chunk = int(density_samples_count / 2)
    gap = 1e0
    gap_samples = 128
    distance_length = 0.001
    distance_threshold = 0.001
    view_disparity = 1e-1
    limit = 1.2
    length = 4
    t = np.linspace(-limit, limit, N)
    sampling_method = 0
    dynamic_disparity = False
    specific_view = False
    adjust_normals = False
    plane_near = 0
    plane_far = 4

    vertices, triangles, normals, diffuse = None, None, None, None
    if config_args.cache_mesh:
        query_pts = np.stack(np.meshgrid(t, t, t), -1).astype(np.float32)
        pts = torch.from_numpy(query_pts)
        dimension = pts.shape[-1]

        pts_flat = pts.reshape((-1, dimension))
        pts_flat_batch = pts_flat.reshape((-1, batch_size, dimension))

        density = np.zeros((pts_flat.shape[0]))
        for idx, batch in enumerate(tqdm(pts_flat_batch)):
            batch = batch.cuda()

            embedded = encode_position_fn(batch)
            if encode_direction_fn is not None:
                embedded_dirs = encode_direction_fn(batch)
                embedded = torch.cat((embedded, embedded_dirs), dim = -1)

            # result_batch = model_fine(embedded)
            result_batch = model_fine(embedded)

            # Extracting the density
            density[idx * batch_size: (idx + 1) * batch_size] = result_batch[..., 3].cpu().detach().numpy()

        # Create a 3D density grid
        grid_alpha = density.reshape((N, N, N))
        print(f"Max density: {grid_alpha.max()}, Min density {grid_alpha.min()}, Mean density {grid_alpha.mean()}")
        print(f"Querying based on iso level: {iso_value}")

        # Extracting iso-surface triangulated
        vertices, triangles, normals, values = measure.marching_cubes(grid_alpha, iso_value)
        vertices = np.ascontiguousarray(vertices)
        normals = np.ascontiguousarray(normals)

        # Query directly without specific-views
        if specific_view:
            targets = torch.from_numpy(vertices) / N * 2 * limit - limit
            targets = targets[:, [1, 0, 2]]
            stride = 512
            diffuse = np.zeros((len(targets), 3))
            for idx in range(0, len(targets) // stride + 1):
                offset1 = stride * idx
                offset2 = np.minimum(stride * (idx + 1), len(vertices))
                batch = targets[offset1:offset2].to(device)

                embedded = encode_position_fn(batch)
                if encode_direction_fn is not None:
                    embedded_dirs = encode_direction_fn(batch)
                    embedded = torch.cat((embedded, embedded_dirs), dim = -1)

                # result_batch = model_fine(embedded)
                result_batch = model_fine(embedded)

                # Query the whole diffuse map
                diffuse[offset1:offset2] = result_batch[..., :3].cpu().detach().numpy()

        if adjust_normals:
            # Re-adjust normals based on NERF's density grid
            # Create a density KDTree look-up table
            tree = KDTree(pts_flat) if sampling_method == 0 else None

            # Create some density samples
            density_samples = np.linspace(-distance_length, distance_length, density_samples_count)[:, np.newaxis]

            # Adjust normals with the assumption of having proper geometry
            print("Adjusting normals")
            for index, vertex in enumerate(tqdm(vertices)):
                vertex_norm = vertex[[1, 0, 2]] / N * 2 * limit - limit
                vertex_direction = normals[index][[1, 0, 2]]

                # Sample points across the ray direction (a.k.a normal)
                samples = vertex_norm[np.newaxis, :].repeat(density_samples_count, 0) + \
                    vertex_direction[np.newaxis, :].repeat(density_samples_count, 0) * density_samples

                def extract_cum_density(samples):
                    inliers_indices = None
                    if sampling_method == 0:
                        # Sample 1th nearest neighbor
                        distances, indices = tree.query(samples, 1)

                        # Filter outliers
                        inliers_indices = indices[distances <= distance_threshold]
                    elif sampling_method == 1:
                        # Sample based on grid proximity
                        indices = (np.around((samples + limit) / 2 / limit * N) * N ** np.arange(2, -1, -1)).sum(1).astype(int)

                        # Filtering exceeding boundaries
                        inliers_indices = indices[~(indices >= N ** 3)]
                    else:
                        # Sample based on re-computing the radiance field
                        indices = (np.around((samples + limit) / 2 / limit * N) * N ** np.arange(2, -1, -1)).sum(1).astype(int)

                        # Filtering exceeding boundaries
                        inliers_indices = indices[~(indices >= N ** 3)]

                    return density[inliers_indices].sum()

                # Extract densities
                sample_density_1 = extract_cum_density(samples[:chunk])
                sample_density_2 = extract_cum_density(samples[chunk:])

                # Re-direct the normal
                if sample_density_1 < sample_density_2:
                    normals[index] *= (-1)

        np.save("mesh_sample.npy", (vertices, triangles, normals))
        print("Saved successfully")
    else:
        vertices, triangles, normals = np.load("mesh_sample.npy", allow_pickle = True)

    # Extracting the diffuse color
    # Ray targets
    targets = torch.from_numpy(vertices) / N * 2 * limit - limit

    # Swap x-axis and y-axis
    targets = targets[:, [1, 0, 2]]

    # Ray directions
    directions = torch.from_numpy(normals)

    # Ray directions swapped based on Marching Cubes algorithm
    directions = directions[:, [1, 0, 2]]

    if dynamic_disparity:
        # Create some density samples
        density_samples = torch.linspace(1e-3, gap, gap_samples).repeat(targets.shape[0], 1)[..., None]
        density_points = targets[:, None, :] + density_samples * directions[:, None, :]
        density_indices = ((density_points + limit) / (2 * limit) * N).long().clamp_(0, N - 1)
        indices = (density_indices.view(-1, 3)[:, [1, 0, 2]] * (N ** torch.arange(2, -1, -1))[None, :]).sum(-1)

        tn_density = torch.from_numpy(density)
        tn_samples = tn_density[indices].view(targets.shape[0], gap_samples)
        # tn_attenut = (tn_samples * cumprod_exclusive(tn_samples >= 0))
        tn_attenut = tn_samples * cumprod_exclusive(torch.relu(tn_samples))
        tn_discrip = (tn_attenut > 1e-13).sum(-1)
        tn_offset = tn_discrip[:, None].float() / gap_samples * gap
        print(tn_discrip[:, None].float().max())
        print((tn_offset > view_disparity).sum())
        print(tn_offset.shape)
        view_disparity = torch.max(tn_offset, torch.ones((targets.shape[0], 1)) * view_disparity)

    # Ray origins
    ray_origins = targets + view_disparity * directions

    # Ray directions
    ray_directions_loose = targets - ray_origins
    ray_directions = ray_directions_loose / ray_directions_loose.norm(dim = 1).unsqueeze(1)

    near = plane_near * torch.ones_like(ray_directions[..., :1])
    far = plane_far * torch.ones_like(ray_directions[..., :1])

    # Generating ray batches
    rays = torch.cat((ray_origins, ray_directions, near, far), dim = -1)
    if cfg.nerf.use_viewdirs:
        # Provide ray directions as input
        view_dirs = ray_directions / ray_directions.norm(p = 2, dim = -1).unsqueeze(-1)
        rays = torch.cat((rays, view_dirs.view((-1, 3))), dim = -1)

    ray_batches = get_minibatches(rays, chunksize = batch_size)

    pred = []
    print("Started ray-casting")
    for ray_batch in tqdm(ray_batches):
        # move to appropriate device
        ray_batch = ray_batch.to(device)

        diffuse_coarse, _, _, _, diffuse_fine, _, _, _ = predict_and_render_radiance(ray_batch, model_coarse, model_fine, cfg,
                mode = "validation",
                encode_position_fn = encode_position_fn,
                encode_direction_fn = encode_direction_fn,
        )

        pred.append(diffuse_fine.cpu().detach())

    # Query the whole diffuse map
    diffuse_fine = torch.cat(pred, dim = 0).numpy()

    # Export model
    export_obj(vertices, triangles, diffuse_fine, normals, "lego.obj")


if __name__ == "__main__":
    main()
