from custom_ops import *
import numpy as np
from utils import generate_vel_list, shape_mask


class GridCell(object):
    def __init__(self, FLAGS):
        self.beta1 = FLAGS.beta1
        self.place_dim = FLAGS.place_dim
        self.block_size = FLAGS.block_size
        self.num_interval = int(np.sqrt(self.place_dim))
        self.num_group = FLAGS.num_group
        self.grid_cell_dim = int(self.num_group * self.block_size)
        self.sigma = np.asarray(FLAGS.sigma, dtype=np.float32)
        self.single_block = FLAGS.single_block
        assert self.num_group * self.block_size == self.grid_cell_dim
        assert self.num_interval * self.num_interval == self.place_dim
        self.shape = FLAGS.shape

        self.max_vel2 = np.sqrt(1.5 / FLAGS.alpha) * (self.num_interval - 1) if self.single_block else FLAGS.max_vel2
        # self.max_vel2 = FLAGS.max_vel2
        self.min_vel2 = FLAGS.min_vel2
        self.velocity2 = generate_vel_list(self.max_vel2)
        self.num_vel2 = len(self.velocity2)
        self.lamda2 = FLAGS.lamda2
        self.lamda3 = FLAGS.lamda3
        self.motion_type = FLAGS.motion_type
        self.num_step = FLAGS.num_step
        self.GandE = FLAGS.GandE
        self.save_memory = FLAGS.save_memory
        self.lr = FLAGS.lr
        self.interval_length = 1.0 / (self.num_interval - 1)

        # initialize A weights
        A_initial = np.random.normal(scale=0.001, size=[self.num_interval, self.num_interval, self.grid_cell_dim])
        self.weights_A = tf.get_variable('A', initializer=tf.convert_to_tensor(A_initial, dtype=tf.float32))
        if self.motion_type == 'discrete':
            self.weights_M = construct_block_diagonal_weights(num_channel=self.num_vel2, num_block=self.num_group, block_size=self.block_size)
        # initialized alpha weights
        if self.single_block:
            self.alpha = tf.convert_to_tensor([FLAGS.alpha], dtype=tf.float32)
        else:
            alpha_initial = np.random.random(size=[self.num_group]) * 110.0
            # alpha_initial = np.random.normal(scale=0.001, size=[self.num_group])
            self.alpha = tf.get_variable('alpha', initializer=tf.convert_to_tensor(alpha_initial, dtype=tf.float32))

    def build_model(self):
        # compute loss1
        self.place_before1 = tf.placeholder(shape=[None, 2], dtype=tf.float32, name='place_before1')
        self.place_after1 = tf.placeholder(shape=[None, 2], dtype=tf.float32, name='place_after1')
        self.vel1 = tf.placeholder(shape=[None], dtype=tf.float32, name='vel1')

        grid_code_before1 = self.get_grid_code(self.place_before1)
        grid_code_after1 = self.get_grid_code(self.place_after1)
        self.dp1 = self.GandE * tf.exp(- self.vel1 ** 2 / self.sigma[0] / self.sigma[0] / 2.0)
        self.dp2 = (1.0 - self.GandE) * tf.exp(- self.vel1 / 0.3)
        displacement = self.dp1 + self.dp2

        self.loss1 = tf.reduce_sum((tf.reduce_sum(grid_code_before1 * grid_code_after1, axis=1) - displacement) ** 2)

        # compute loss2
        # motion_init = self.construct_motion_matrix(self.vel2[:, 0])
        self.place_seq2 = tf.placeholder(shape=[None, self.num_step + 1, 2], dtype=tf.float32, name='place_seq2')
        if self.motion_type == 'continuous':
            self.vel2 = tf.placeholder(shape=[None, self.num_step, 2], dtype=tf.float32, name='vel2')
        else:
            self.vel2 = tf.placeholder(shape=[None, self.num_step], dtype=tf.int32, name='vel2')

        self.lamda = tf.placeholder(dtype=tf.float32)
        grid_code_seq2 = self.get_grid_code(self.place_seq2)
        grid_code = grid_code_seq2[:, 0]
        loss2 = tf.constant(0.0)
        for step in range(self.num_step):
            current_M = self.construct_motion_matrix(self.vel2[:, step], reuse=tf.AUTO_REUSE)
            grid_code = self.motion_model(current_M, grid_code)
            loss2 = loss2 + tf.reduce_sum(tf.square(grid_code - grid_code_seq2[:, step+1]))

        # self.loss2 = loss2
        self.loss2 = self.lamda * loss2
        grid_code_end_pd = grid_code

        self.place_end_pd, _ = self.localization_model(self.weights_A, grid_code_end_pd, self.grid_cell_dim)
        self.place_start_infer, _ = self.localization_model(self.weights_A, grid_code_seq2[:, 0], self.grid_cell_dim)
        self.place_end_infer, _ = self.localization_model(self.weights_A, grid_code_seq2[:, -1], self.grid_cell_dim)
        self.place_start_gt, self.place_end_gt = self.place_seq2[:, 0], self.place_seq2[:, -1]

        # compute loss 3
        self.place_seq3 = tf.placeholder(shape=[None, self.num_group, 2, 2], dtype=tf.float32, name='place_seq3')
        self.loss3 = self.compute_loss3(self.place_seq3)
        self.loss4 = tf.reduce_sum(tf.abs(tf.reduce_sum(self.weights_A ** 2, axis=2) - 1.0))

        # compute total loss
        A_reshape = tf.reshape(self.weights_A, [self.place_dim, self.grid_cell_dim])
        mask = np.reshape(shape_mask(self.num_interval, self.shape), [-1])
        A_reshape_mask = tf.boolean_mask(A_reshape, mask, axis=0)

        self.reg = self.lamda2 * tf.reduce_sum((tf.reduce_sum(A_reshape_mask ** 2, axis=0) / np.sum(mask)
                                                - 1.0 / self.grid_cell_dim) ** 2)

        if self.single_block:
            self.loss = self.loss2 + self.loss3 + self.reg
        else:
            self.loss = self.loss1 + self.loss2 + self.reg + self.loss3
            # self.loss = self.loss3
        self.loss_mean, self.loss_update = tf.contrib.metrics.streaming_mean(self.loss)

        # optim = tf.train.MomentumOptimizer(self.lr, 0.9)
        optim = tf.train.AdamOptimizer(self.lr, beta1=self.beta1)
        trainable_vars = tf.trainable_variables()
        self.apply_grads = optim.minimize(self.loss, var_list=trainable_vars)

        tf.summary.scalar('loss', self.loss)
        tf.summary.scalar('loss1', self.loss1)
        tf.summary.scalar('loss2', self.loss2)
        tf.summary.scalar('loss3', self.loss3)
        tf.summary.scalar('loss4', self.loss4)

        self.summary_op = tf.summary.merge_all()
        self.norm_grads = tf.assign(self.weights_A, tf.nn.l2_normalize(self.weights_A, axis=2))
        self.I2 = tf.matmul(A_reshape, A_reshape, transpose_b=True)

    def compute_loss3(self, place_seq):
        loss = tf.constant(0.0)
        for block_idx in range(self.num_group):
            block_slice = np.arange(block_idx * self.block_size, (block_idx + 1) * self.block_size)
            place_seq_block = tf.gather(place_seq, block_idx, axis=1)
            grid_code = self.get_grid_code(place_seq_block)
            grid_code_block = tf.gather(grid_code, block_slice, axis=-1)
            alpha_block = tf.gather(self.alpha, block_idx)
            displacement = (place_seq_block[:, 0] - place_seq_block[:, 1]) * self.interval_length
            local_kernel = (1 - alpha_block * tf.reduce_sum(displacement ** 2, axis=-1)) / self.num_group
            grid_code_block_inner_product = tf.reduce_sum(grid_code_block[:, 0] * grid_code_block[:, 1], axis=-1)
            self.local_kernel = local_kernel
            self.grid_code_block_inner_product = grid_code_block_inner_product

            loss = loss + tf.reduce_sum((local_kernel - grid_code_block_inner_product) ** 2)

        return loss * self.lamda3

    # def compute_loss3(self, place_seq):
    #     grid_code_seq = self.get_grid_code(place_seq)
    #     # grid_code_seq = tf.reshape(grid_code_seq, [tf.shape(place_seq)[0], tf.shape(place_seq)[1], tf.shape(place_seq)[2], self.grid_cell_dim])
    #     grid_code_inner_product = grid_code_seq[:, :, 0] * grid_code_seq[:, :, 1]
    #     grid_code_inner_product_group = tf.reduce_sum(
    #         tf.reshape(grid_code_inner_product, [-1, self.num_group, self.num_group, self.block_size]), axis=-1)
    #     grid_code_inner_product_group = tf.linalg.diag_part(grid_code_inner_product_group)
    #
    #     displacement = tf.reduce_sum(((place_seq[:, :, 0] - place_seq[:, :, 1]) * self.interval_length) ** 2, axis=-1)
    #     local_kernel = (1.0 - self.alpha * displacement) / self.num_group
    #
    #     loss = 5.0 * tf.reduce_sum((grid_code_inner_product_group - local_kernel) ** 2)
    #
    #     return loss

    def get_grid_code(self, place_):
        grid_code = tf.contrib.resampler.resampler(tf.transpose(
            tf.expand_dims(self.weights_A, axis=0), perm=[0, 2, 1, 3]), tf.expand_dims(place_, axis=0))
        grid_code = tf.squeeze(grid_code)
        return grid_code

    def construct_motion_matrix(self, vel_, reuse=None):
        with tf.variable_scope('M', reuse=reuse):
            if self.motion_type == 'continuous':
                vel = tf.reshape(vel_, [-1, 2])
                input_reform = tf.concat([vel, vel ** 2, tf.expand_dims(vel[:, 0] * vel[:, 1], axis=1)], axis=1)

                output = tf.layers.dense(input_reform, self.num_group * self.block_size * self.block_size, use_bias=False,
                                         kernel_initializer=tf.random_normal_initializer(stddev=0.01), name='fc1')
                if self.save_memory:
                    current_M = tf.reshape(output, [-1, self.num_group, self.block_size, self.block_size])
                else:
                    output = tf.reshape(output, [-1, self.num_group, self.block_size, self.block_size])
                    output = tf.unstack(output, axis=1)
                    current_M = block_diagonal(output)
            else:
                current_M = tf.gather(self.weights_M, vel_)

            return tf.squeeze(current_M)

    def motion_model(self, M, grid_code):
        # M = self.construct_motion_matrix(vel, reuse=tf.AUTO_REUSE)
        if self.save_memory:
            indices = np.reshape(np.arange(self.grid_cell_dim), [self.num_group, self.block_size])
            grid_code_gp = tf.expand_dims(tf.gather(grid_code, indices, axis=-1), axis=-1)
            grid_code_new = tf.matmul(M + tf.diag(tf.ones(self.block_size)), grid_code_gp)
            grid_code_new = tf.reshape(grid_code_new, [-1, self.grid_cell_dim])
        else:
            grid_code_new = tf.matmul(M + tf.diag(tf.ones(self.grid_cell_dim)), tf.expand_dims(grid_code, -1))
        return tf.squeeze(grid_code_new)

    def localization_model(self, A, grid_code_, grid_cell_dim, pd_pt=False):
        grid_code = tf.reshape(grid_code_, [-1, grid_cell_dim])
        A_reshape = tf.reshape(A, [-1, grid_cell_dim])
        place_code = tf.matmul(A_reshape, grid_code, transpose_b=True)
        place_pt_pd = None
        # place_pt_pd = tf.argmax(place_code, axis=0)

        place_code = tf.transpose(
            tf.reshape(place_code, [self.num_interval, self.num_interval, -1]), perm=[2, 0, 1])
        if pd_pt:
            place_quantile = tf.contrib.distributions.percentile(place_code, 98)
            place_pt_pool = tf.where(place_code - place_quantile >= 0)
            place_pt_pd_x = tf.contrib.distributions.percentile(place_pt_pool[:, 1], 50.0)
            place_pt_pd_y = tf.contrib.distributions.percentile(place_pt_pool[:, 2], 50.0)
            place_pt_pd = tf.stack((place_pt_pd_x, place_pt_pd_y))
            place_pt_pd = tf.cast(place_pt_pd, tf.float32)

            # place_pd_idx = tf.argmax(tf.reshape(place_code, [-1]))
            # place_pt_pd = tf.cast(tf.transpose([tf.floordiv(place_pd_idx, self.num_interval),
            #                                     tf.mod(place_pd_idx, self.num_interval)]), tf.int32)

        # place_pt_pd = tf.squeeze(tf.stack([tf.floordiv(place_pt_pd, self.num_interval), tf.mod(place_pt_pd, self.num_interval)], axis=1))

        return tf.squeeze(place_code), place_pt_pd

    def path_integral(self, test_num, project_to_point=False):
        # build testing model
        with tf.name_scope("path_integral"):
            self.place_init_test = tf.placeholder(shape=[2], dtype=tf.float32)
            if self.motion_type == 'continuous':
                self.vel2_test = tf.placeholder(shape=[test_num, 2], dtype=tf.float32)
            else:
                self.vel2_test = tf.placeholder(shape=[test_num], dtype=tf.int32)

            place_seq_pd = []
            place_seq_pd_pt = []
            place_seq_pd_gp = []
            # grid_code = tf.squeeze(tf.contrib.resampler.resampler(tf.transpose(
            #     tf.expand_dims(self.weights_A, axis=0), perm=[0, 2, 1, 3]), tf.expand_dims(self.place_init_test, axis=0)))
            grid_code = self.get_grid_code(self.place_init_test)

            place_pd, place_pd_pt = self.localization_model(self.weights_A, grid_code, self.grid_cell_dim, pd_pt=True)
            place_seq_pd.append(place_pd)
            place_seq_pd_pt.append(place_pd_pt)

            place_seq_pd_gp_list = []
            for gp in range(self.num_group):
                gp_id = slice(gp * self.block_size, (gp + 1) * self.block_size)
                place_pd_gp, _ = self.localization_model(
                    tf.nn.l2_normalize(self.weights_A[:, :, gp_id], axis=-1), tf.nn.l2_normalize(grid_code[gp_id]),
                    self.block_size)
                place_seq_pd_gp_list.append(place_pd_gp)
            place_seq_pd_gp.append(tf.stack(place_seq_pd_gp_list))

            for step in range(test_num):
                # current_M = self.construct_motion_matrix(self.vel2_test[step], reuse=tf.AUTO_REUSE)
                if project_to_point is True and step > 0:
                    grid_code = self.get_grid_code(place_seq_pd_pt[-1])
                M = self.construct_motion_matrix(self.vel2_test[step], reuse=tf.AUTO_REUSE)
                grid_code = self.motion_model(M, grid_code)

                place_pd, place_pd_pt = self.localization_model(self.weights_A, grid_code, self.grid_cell_dim, pd_pt=True)

                place_seq_pd.append(place_pd)
                place_seq_pd_pt.append(place_pd_pt)

                place_seq_pd_gp_list = []
                for gp in range(self.num_group):
                    gp_id = slice(gp * self.block_size, (gp + 1) * self.block_size)
                    place_pd_gp, _ = self.localization_model(
                        tf.nn.l2_normalize(self.weights_A[:, :, gp_id], axis=-1), tf.nn.l2_normalize(grid_code[gp_id]),
                        self.block_size)
                    place_seq_pd_gp_list.append(place_pd_gp)
                place_seq_pd_gp.append(tf.stack(place_seq_pd_gp_list))

            self.place_seq_pd, self.place_seq_pd_pt, self.place_seq_pd_gp = \
                tf.stack(place_seq_pd), tf.stack(place_seq_pd_pt), tf.stack(place_seq_pd_gp)

