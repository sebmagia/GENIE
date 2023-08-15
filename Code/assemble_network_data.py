import yaml
import numpy as np
from matplotlib import pyplot as plt
import os
import torch
from torch import optim, nn
import shutil
from scipy.spatial import cKDTree
from utils import *

## User: Input stations and spatial region
## (must have station and region files at
## (ext_dir + 'stations.npz'), and
## (ext_dir + 'region.npz')

# ext_dir = 'D:/Projects/Mayotte/Mayotte/' ## Replace with absolute directory to location to setup folders, and where all the ".py" files from Github are located.

# Load configuration from YAML
with open('config.yaml', 'r') as file:
    config = yaml.safe_load(file)
    
name_of_project = 'Mayotte' ## Replace with the name of project (a single word is prefered).

path_to_file = str(pathlib.Path().absolute())
path_to_file += '\\' if '\\' in path_to_file else '/'

print(path_to_file)

# Station file

# z = np.load(ext_dir + '%s_stations.npz'%name_of_project)
z = np.load(path_to_file + 'stations.npz')
locs, stas = z['locs'], z['stas']
z.close()

print('\n Using stations:')
print(stas)
print('\n Using locations:')
print(locs)

# Region file
# z = np.load(ext_dir + '%s_region.npz'%name_of_project)
z = np.load(path_to_file + 'region.npz', allow_pickle = True)
lat_range, lon_range, depth_range = z['lat_range'], z['lon_range'], z['depth_range'], 
deg_pad, num_grids, years = z['deg_pad'], z['num_grids'], z['years']
n_spatial_nodes = z['n_spatial_nodes']
load_initial_files = z['load_initial_files'][0]
use_pretrained_model = z['use_pretrained_model'][0]

if use_pretrained_model == 'None':
    use_pretrained_model = None

z.close()
shutil.copy(path_to_file + 'region.npz', path_to_file + f'{config['name_of_project']}_region.npz')

with_density = None
use_spherical = False ## Should only set to true if travel time model also has spherical projection (to be added soon)
depth_importance_weighting_value_for_spatial_graphs = 2.5 # up scale the depth importance of node positions when creating spatial 
## graph if using a large horizontally extended domain

# else, set with_density = srcs with srcs[:,0] == lat, srcs[:,1] == lon, srcs[:,2] == depth
## to preferentially focus the spatial graphs closer around reference sources. 

## Fit projection coordinates and create spatial grids

def optimize_with_differential_evolution(center_loc, nominal_depth = 0.0):

	loss_coef = [1,1,1.0,0]

	unit_lat = np.array([0.001, 0.0, 0.0]).reshape(1,-1) + center_loc
	unit_vert = np.array([0.0, 0.0, 10.0]).reshape(1,-1) + center_loc

	### LOOK HERE ###
	norm_lat = np.linalg.norm(np.diff(lla2ecef(np.concatenate((center_loc, unit_lat), axis = 0)), axis = 0), axis = 1)
	norm_vert = np.linalg.norm(np.diff(lla2ecef(np.concatenate((center_loc, unit_vert), axis = 0)), axis = 0), axis = 1)
	### LOOK HERE ###
	
	trgt_lat = np.array([0,1.0,0]).reshape(1,-1)
	trgt_vert = np.array([0,0,1.0]).reshape(1,-1)
	trgt_center = np.zeros(3)

	def loss_function(x):

		rbest = rotation_matrix_full_precision(x[0], x[1], x[2])

		norm_lat = lla2ecef(np.concatenate((center_loc, unit_lat), axis = 0))
		norm_vert = lla2ecef(np.concatenate((center_loc, unit_vert), axis = 0))
		norm_lat = np.linalg.norm(norm_lat[1] - norm_lat[0])
		norm_vert = np.linalg.norm(norm_vert[1] - norm_vert[0])

		center_out = ftrns1(center_loc, rbest, x[3::].reshape(1,-1))

		out_unit_lat = ftrns1(unit_lat, rbest, x[3::].reshape(1,-1))
		out_unit_lat = (out_unit_lat - center_out)/norm_lat

		out_unit_vert = ftrns1(unit_vert, rbest, x[3::].reshape(1,-1))
		out_unit_vert = (out_unit_vert - center_out)/norm_vert

		out_locs = ftrns1(locs, rbest, x[3::].reshape(1,-1))

		loss1 = np.linalg.norm(trgt_lat - out_unit_lat, axis = 1)
		loss2 = np.linalg.norm(trgt_vert - out_unit_vert, axis = 1)
		loss3 = np.linalg.norm(trgt_center.reshape(1,-1) - center_out, axis = 1) ## Scaling loss down
		loss = loss_coef[0]*loss1 + loss_coef[1]*loss2 + loss_coef[2]*loss3 # + loss_coef[3]*loss4

		return loss

	bounds = [(0, 2.0*np.pi), (0, 2.0*np.pi), (0, 2.0*np.pi), (-1e7, 1e7), (-1e7, 1e7), (-1e7, 1e7)]

	soln = differential_evolution(lambda x: loss_function(x), bounds, popsize = 30, maxiter = 1000, disp = True)

	# soln = direct(lambda x: loss_function(x), bounds, maxfun = int(100e3), maxiter = int(10e3))

	return soln

def assemble_grids(scale_x_extend, offset_x_extend, n_grids, n_cluster, n_steps = 5000, extend_grids = True, with_density = None, density_kernel = 0.15):

	if with_density is not None:
		from sklearn.neighbors import KernelDensity
		m_density = KernelDensity(kernel = 'gaussian', bandwidth = density_kernel).fit(with_density[:,0:2])

	x_grids = []
	for i in range(n_grids):

		eps_extra = 0.1
		eps_extra_depth = 0.02
		scale_up = 1.0 # 10000.0
		weight_vector = np.array([1.0, 1.0, depth_importance_weighting_value_for_spatial_graphs]).reshape(1,-1) ## Tries to scale importance of depth up, so that nodes fill depth-axis well

		offset_x_extend_slice = np.array([offset_x_extend[0,0], offset_x_extend[0,1], offset_x_extend[0,2]]).reshape(1,-1)
		scale_x_extend_slice = np.array([scale_x_extend[0,0], scale_x_extend[0,1], scale_x_extend[0,2]]).reshape(1,-1)

		depth_scale = (np.diff(depth_range)*0.02)
		deg_scale = ((0.5*np.diff(lat_range) + 0.5*np.diff(lon_range))*0.08)

		if extend_grids == True:
			extend1, extend2, extend3, extend4 = (np.random.rand(4) - 0.5)*deg_scale
			extend5 = (np.random.rand() - 0.5)*depth_scale
			offset_x_extend_slice[0,0] += extend1
			offset_x_extend_slice[0,1] += extend2
			scale_x_extend_slice[0,0] += extend3
			scale_x_extend_slice[0,1] += extend4
			offset_x_extend_slice[0,2] += extend5
			scale_x_extend_slice[0,2] = depth_range[1] - offset_x_extend_slice[0,2]

		else:
			pass

		print('\n Optimize for spatial grid (%d / %d)'%(i + 1, n_grids))

		offset_x_grid = scale_up*np.array([offset_x_extend_slice[0,0] - eps_extra*scale_x_extend_slice[0,0], offset_x_extend_slice[0,1] - eps_extra*scale_x_extend_slice[0,1], offset_x_extend_slice[0,2] - eps_extra_depth*scale_x_extend_slice[0,2]]).reshape(1,-1)
		scale_x_grid = scale_up*np.array([scale_x_extend_slice[0,0] + 2.0*eps_extra*scale_x_extend_slice[0,0], scale_x_extend_slice[0,1] + 2.0*eps_extra*scale_x_extend_slice[0,1], scale_x_extend_slice[0,2] + 2.0*eps_extra_depth*scale_x_extend_slice[0,2]]).reshape(1,-1)
	
		if with_density is not None:

			x_grid = kmeans_packing_weight_vector_with_density(m_density, weight_vector, scale_x_grid, offset_x_grid, 3, n_cluster, ftrns1, n_batch = 10000, n_steps = n_steps, n_sim = 1, lr = 0.005)[0]/scale_up # .to(device) # 8000

		else:
			x_grid = kmeans_packing_weight_vector(weight_vector, scale_x_grid, offset_x_grid, 3, n_cluster, ftrns1, n_batch = 10000, n_steps = n_steps, n_sim = 1, lr = 0.005)[0]/scale_up # .to(device) # 8000

		iargsort = np.argsort(x_grid[:,0])
		x_grid = x_grid[iargsort]
		x_grids.append(x_grid)

	return x_grids # , x_grids_edges

if use_spherical == True:

	earth_radius = 6371e3
	ftrns1 = lambda x, rbest, mn: (rbest @ (lla2ecef(x, e = 0.0, a = earth_radius) - mn).T).T # just subtract mean
	ftrns2 = lambda x, rbest, mn: ecef2lla((rbest.T @ x.T).T + mn, e = 0.0, a = earth_radius) # just subtract mean

else:

	earth_radius = 6378137.0
	ftrns1 = lambda x, rbest, mn: (rbest @ (lla2ecef(x) - mn).T).T # just subtract mean
	ftrns2 = lambda x, rbest, mn: ecef2lla((rbest.T @ x.T).T + mn) # just subtract mean

## Unit lat, vertical vectors; point positive y, and outward normal
## mean centered stations. Keep the vertical depth, consistent.

print('\n Using domain')
print('\n Latitude:')
print(lat_range)
print('\n Longitude:')
print(lon_range)

fix_nominal_depth = True
if fix_nominal_depth == True:
	nominal_depth = 0.0 ## Can change the target depth projection if prefered
else:
	nominal_depth = locs[:,2].mean() ## Can change the target depth projection if prefered

center_loc = np.array([lat_range[0] + 0.5*np.diff(lat_range)[0], lon_range[0] + 0.5*np.diff(lon_range)[0], nominal_depth]).reshape(1,-1)
# center_loc = locs.mean(0, keepdims = True)

use_differential_evolution = True
if use_differential_evolution == True:

	## This is prefered fitting method
	
	## Unit lat, vertical vectors; point positive y, and outward normal
	## mean centered stations. Keep the vertical depth, consistent.
	
	print('\n Using domain')
	print('\n Latitude:')
	print(lat_range)
	print('\n Longitude:')
	print(lon_range)
	
	fix_nominal_depth = True
	assert(fix_nominal_depth == True)
	if fix_nominal_depth == True:
		nominal_depth = 0.0 ## Can change the target depth projection if prefered
	else:
		nominal_depth = locs[:,2].mean() ## Can change the target depth projection if prefered
	
	center_loc = np.array([lat_range[0] + 0.5*np.diff(lat_range)[0], lon_range[0] + 0.5*np.diff(lon_range)[0], nominal_depth]).reshape(1,-1)
	
	from scipy.optimize import differential_evolution
	
	# os.rename(ext_dir + 'stations.npz', ext_dir + '%s_stations_backup.npz'%name_of_project)
	
	soln = optimize_with_differential_evolution(center_loc)
	rbest = rotation_matrix_full_precision(soln.x[0], soln.x[1], soln.x[2])
	mn = soln.x[3::].reshape(1,-1)

else:

	## If optimizing projection coefficients with this option, need 
	## ftrns1 and ftrns2 to accept torch Tensors instead of numpy arrays
	if use_spherical == True:

		earth_radius = 6371e3
		ftrns1 = lambda x, rbest, mn: (rbest @ (lla2ecef_diff(x, e = 0.0, a = earth_radius) - mn).T).T # just subtract mean
		ftrns2 = lambda x, rbest, mn: ecef2lla_diff((rbest.T @ x.T).T + mn, e = 0.0, a = earth_radius) # just subtract mean
	
	else:
	
		earth_radius = 6378137.0
		ftrns1 = lambda x, rbest, mn: (rbest @ (lla2ecef_diff(x) - mn).T).T # just subtract mean
		ftrns2 = lambda x, rbest, mn: ecef2lla_diff((rbest.T @ x.T).T + mn) # just subtract mean
	
	## Iterative optimization, does not converge as well

	n_attempts = 10

	unit_lat = np.array([0.01, 0.0, 0.0]).reshape(1,-1) + center_loc
	unit_vert = np.array([0.0, 0.0, 1000.0]).reshape(1,-1) + center_loc
	
	norm_lat = torch.Tensor(np.linalg.norm(np.diff(lla2ecef(np.concatenate((center_loc, unit_lat), axis = 0)), axis = 0), axis = 1))
	norm_vert = torch.Tensor(np.linalg.norm(np.diff(lla2ecef(np.concatenate((center_loc, unit_vert), axis = 0)), axis = 0), axis = 1))
	
	trgt_lat = torch.Tensor([0,1.0,0]).reshape(1,-1)
	trgt_vert = torch.Tensor([0,0,1.0]).reshape(1,-1)
	trgt_center = torch.zeros(2)
	
	loss_func = nn.MSELoss()
	
	losses = []
	losses1, losses2, losses3, losses4 = [], [], [], []
	loss_coef = [1,1,1,0]
	
	## Based on initial conditions, sometimes this converges to a projection plane that is flipped polarity.
	## E.g., "up" in the lat-lon domain is "down" in the Cartesian domain, and vice versa.
	## So try a few attempts to make sure it has the correct polarity.
	for attempt in range(n_attempts):
	
		vec = nn.Parameter(2.0*np.pi*torch.rand(3))
	
		# mn = nn.Parameter(torch.Tensor(lla2ecef(locs).mean(0, keepdims = True)))
		mn = nn.Parameter(torch.Tensor(lla2ecef(center_loc.reshape(1,-1)).mean(0, keepdims = True)))
	
		optimizer = optim.Adam([vec, mn], lr = 0.001)
	
		print('\n Optimize the projection coefficients \n')
	
		n_steps_optimize = 5000
		for i in range(n_steps_optimize):
	
			optimizer.zero_grad()
	
			rbest = rotation_matrix(vec[0], vec[1], vec[2])
	
			norm_lat = lla2ecef_diff(torch.Tensor(np.concatenate((center_loc, unit_lat), axis = 0)))
			norm_vert = lla2ecef_diff(torch.Tensor(np.concatenate((center_loc, unit_vert), axis = 0)))
			norm_lat = torch.norm(norm_lat[1] - norm_lat[0])
			norm_vert = torch.norm(norm_vert[1] - norm_vert[0])
	
			center_out = ftrns1(torch.Tensor(center_loc), rbest, mn)
	
			out_unit_lat = ftrns1(torch.Tensor(unit_lat), rbest, mn)
			out_unit_lat = (out_unit_lat - center_out)/norm_lat
	
			out_unit_vert = ftrns1(torch.Tensor(unit_vert), rbest, mn)
			out_unit_vert = (out_unit_vert - center_out)/norm_vert
	
			out_locs = ftrns1(torch.Tensor(locs), rbest, mn)
	
			loss1 = loss_func(trgt_lat, out_unit_lat)
			loss2 = loss_func(trgt_vert, out_unit_vert)
			loss3 = loss_func(0.1*trgt_center, 0.1*center_out[0,0:2]) ## Scaling loss down
	
			loss = loss_coef[0]*loss1 + loss_coef[1]*loss2 + loss_coef[2]*loss3 # + loss_coef[3]*loss4
			loss.backward()
			optimizer.step()
	
			losses.append(loss.item())
			losses1.append(loss1.item())
			losses2.append(loss2.item())
			losses3.append(loss3.item())
	
			if np.mod(i, 50) == 0:
				print('%d %0.8f'%(i, loss.item()))
	
		## Save approriate files and make extensions for directory
		print('\n Loss of lat and lon: %0.4f \n'%(loss_coef[0]*loss1 + loss_coef[1]*loss2))
	
		if (loss_coef[0]*loss1 + loss_coef[1]*loss2) < 1e-1:
			print('\n Finished converging \n')
			break
		else:
			print('\n Did not converge, restarting (%d) \n'%attempt)
	
	# os.rename(ext_dir + 'stations.npz', ext_dir + '%s_stations_backup.npz'%name_of_project)
	
	rbest = rbest.cpu().detach().numpy()
	mn = mn.cpu().detach().numpy()

if use_pretrained_model is not None:
	shutil.move(path_to_file + 'Pretrained/trained_gnn_model_step_%d_ver_%d.h5'%(20000, use_pretrained_model), path_to_file + 'GNN_TrainedModels/%s_trained_gnn_model_step_%d_ver_%d.h5'%(config['name_of_project'], 20000, 1))
	shutil.move(path_to_file + 'Pretrained/1d_travel_time_grid_ver_%d.npz'%use_pretrained_model, path_to_file + '1D_Velocity_Models_Regional/%s_1d_travel_time_grid_ver_%d.npz'%(config['name_of_project'], 1))
	shutil.move(path_to_file + 'Pretrained/seismic_network_templates_ver_%d.npz'%use_pretrained_model, path_to_file + 'Grids/%s_seismic_network_templates_ver_%d.npz'%(use_pretrained_model, 1))

	## Find offset corrections if using one of the pre-trained models
	## Load these and apply offsets for runing "process_continuous_days.py"
	z = np.load(path_to_file + 'Pretrained/stations_ver_%d.npz'%use_pretrained_model)['locs']
	sta_loc, rbest, mn = z['locs'], z['rbest'], z['mn']
	corr1 = locs.mean(0, keepdims = True)
	corr2 = sta_loc.mean(0, keepdims = True)
	z.close()

	z = np.load(path_to_file + 'Pretrained/region_ver_%d.npz'%use_pretrained_model)
	lat_range, lon_range, depth_range, deg_pad = z['lat_range'], z['lon_range'], z['depth_range'], z['deg_pad']
	z.close()

	locs = np.copy(locs) - corr1 + corr2
	shutil.copy(path_to_file + 'Pretrained/region_ver_%d.npz'%use_pretrained_model, path_to_file + f'{config['name_of_project']}_region.npz')

else:
	corr1 = np.array([0.0, 0.0, 0.0]).reshape(1,-1)
	corr2 = np.array([0.0, 0.0, 0.0]).reshape(1,-1)

np.savez_compressed(path_to_file + f'{config['name_of_project']}_stations.npz', locs = locs, stas = stas, rbest = rbest, mn = mn)

## Make necessary directories

os.mkdir(path_to_file + 'Picks')
os.mkdir(path_to_file + 'Catalog')
for year in years:
	os.mkdir(path_to_file + 'Picks/%d'%year)
	os.mkdir(path_to_file + 'Catalog/%d'%year)

os.mkdir(path_to_file + 'Plots')
os.mkdir(path_to_file + 'GNN_TrainedModels')
os.mkdir(path_to_file + 'Grids')
os.mkdir(path_to_file + '1D_Velocity_Models_Regional')

if (load_initial_files == True)*(use_pretrained_model == False):
	step_load = 20000
	ver_load = 1
	if os.path.exists(path_to_file + 'trained_gnn_model_step_%d_ver_%d.h5'%(step_load, ver_load)):
		shutil.move(path_to_file + 'trained_gnn_model_step_%d_ver_%d.h5'%(step_load, ver_load), path_to_file + 'GNN_TrainedModels/%s_trained_gnn_model_step_%d_ver_%d.h5'%(config['name_of_project'], step_load, ver_load))

	ver_load = 1
	if os.path.exists(path_to_file + '1d_travel_time_grid_ver_%d.npz'%ver_load):
		shutil.move(path_to_file + '1d_travel_time_grid_ver_%d.npz'%ver_load, path_to_file + '1D_Velocity_Models_Regional/%s_1d_travel_time_grid_ver_%d.npz'%(config['name_of_project'], ver_load))

	ver_load = 1
	if os.path.exists(path_to_file + 'seismic_network_templates_ver_%d.npz'%ver_load):
		shutil.move(path_to_file + 'seismic_network_templates_ver_%d.npz'%ver_load, path_to_file + 'Grids/%s_seismic_network_templates_ver_%d.npz'%(config['name_of_project'], ver_load))

## Make spatial grids

if use_spherical == True:

	earth_radius = 6371e3
	ftrns1 = lambda x: (rbest @ (lla2ecef(x, e = 0.0, a = earth_radius) - mn).T).T # map (lat,lon,depth) into local cartesian (x || East,y || North, z || Outward)
	ftrns2 = lambda x: ecef2lla((rbest.T @ x.T).T + mn, e = 0.0, a = earth_radius)  # invert ftrns1

else:

	earth_radius = 6378137.0	
	ftrns1 = lambda x: (rbest @ (lla2ecef(x) - mn).T).T # map (lat,lon,depth) into local cartesian (x || East,y || North, z || Outward)
	ftrns2 = lambda x: ecef2lla((rbest.T @ x.T).T + mn)  # invert ftrns1
	

lat_range_extend = [lat_range[0] - deg_pad, lat_range[1] + deg_pad]
lon_range_extend = [lon_range[0] - deg_pad, lon_range[1] + deg_pad]

scale_x = np.array([lat_range[1] - lat_range[0], lon_range[1] - lon_range[0], depth_range[1] - depth_range[0]]).reshape(1,-1)
offset_x = np.array([lat_range[0], lon_range[0], depth_range[0]]).reshape(1,-1)
scale_x_extend = np.array([lat_range_extend[1] - lat_range_extend[0], lon_range_extend[1] - lon_range_extend[0], depth_range[1] - depth_range[0]]).reshape(1,-1)
offset_x_extend = np.array([lat_range_extend[0], lon_range_extend[0], depth_range[0]]).reshape(1,-1)

skip_making_grid = False
if load_initial_files == True:
	if os.path.exists('Grids/%s_seismic_network_templates_ver_%d.npz'%(config['name_of_project'], ver_load)) == True:
		skip_making_grid = True

if skip_making_grid == False:
	x_grids = assemble_grids(scale_x_extend, offset_x_extend, num_grids, n_spatial_nodes, n_steps = 5000, with_density = with_density)

	np.savez_compressed(path_to_file + 'Grids/%s_seismic_network_templates_ver_1.npz'%config['name_of_project'], x_grids = [x_grids[i] for i in range(len(x_grids))], corr1 = corr1, corr2 = corr2)