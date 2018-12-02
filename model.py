import numpy as np
import torch
import utils
from torch.autograd import Variable
from data_loader import DataLoader
from hyperparameters import HyperParameters
from discriminator import Discriminator
from generator import Generator

from os import listdir
from os.path import isfile, join

Tensor = torch.cuda.FloatTensor


class Model:

    def __init__(self, dataset_name):
        self.hps = HyperParameters()
        self.dataset_name = dataset_name
        self.generator = Generator().cuda()
        self.discriminator = Discriminator().cuda()
        self.data = np.load(self.hps.data_path.format(self.dataset_name), encoding='latin1')["train"]
        self.data_loader = DataLoader(self.data)
        self.N_max = self.data_loader.N_max

    def get_ground_truth(self, batch, sequence_lengths):
        end_of_sequence = Variable(torch.stack([torch.Tensor([0, 0, 0, 0, 1])] * batch.size()[1]).cuda()).unsqueeze(0)
        batch = torch.cat([batch, end_of_sequence], 0)
        mask = torch.zeros(self.N_max + 1, batch.size()[1])

        # SOS [0,0,1,0,0] should not be considered when compute reconstruction loss
        for sequence_index, sequence_length in enumerate(sequence_lengths):
            mask[1:sequence_length + 1, sequence_index] = 1
        mask = Variable(mask.cuda()).detach()
        delta_x = torch.stack([Variable(batch.data[1:, :, 0])] * self.hps.num_mixture, 2).detach()
        delta_y = torch.stack([Variable(batch.data[1:, :, 1])] * self.hps.num_mixture, 2).detach()
        pen_state = torch.stack([Variable(batch.data[1:, :, 2].detach()),
                                 Variable(batch.data[1:, :, 3].detach()),
                                 Variable(batch.data[1:, :, 4].detach())], 2)
        return mask, delta_x, delta_y, pen_state

    def compute_bivariate_normal_pdf(self, delta_x, delta_y, mu_x, mu_y, sigma_x, sigma_y, rho_xy):
        z_x = ((delta_x - mu_x) / sigma_x) ** 2
        z_y = ((delta_y - mu_y) / sigma_y) ** 2
        z_xy = (2 * rho_xy * (delta_x - mu_x) / sigma_x * (delta_y - mu_y) / sigma_y)
        exp = torch.exp((z_x + z_y - z_xy) / (-2 * (1 - rho_xy ** 2)))
        const = 1 / (2 * np.pi * sigma_x * sigma_y * torch.sqrt(1 - rho_xy ** 2))
        return const * exp

    def compute_reconstruction_loss(self, mask, delta_x, delta_y, pen_state, pi, mu_x, mu_y, sigma_x, sigma_y, rho_xy,
                                    q):
        bivariate_normal_pdf = self.compute_bivariate_normal_pdf(delta_x, delta_y, mu_x, mu_y, sigma_x, sigma_y, rho_xy)
        Ls = -torch.sum(mask * torch.log(self.hps.epsilon + torch.sum(pi * bivariate_normal_pdf, 2))) / float(
            (self.N_max + 1) * self.hps.batch_size)
        Lp = -torch.sum(pen_state * torch.log(q)) / float((self.N_max + 1) * self.hps.batch_size)
        return Ls + Lp, Ls, Lp

    def train(self):

        # Optimizers
        optimizer_G = torch.optim.Adam(self.generator.parameters(), lr=self.hps.lr)
        optimizer_D = torch.optim.Adam(self.discriminator.parameters(), lr=self.hps.lr)

        for i in range(self.hps.num_epoch):
            criterion_GAN = torch.nn.MSELoss()
            valid = Variable(Tensor(np.ones(self.hps.batch_size)), requires_grad=False)
            fake = Variable(Tensor(np.zeros(self.hps.batch_size)), requires_grad=False)

            batch, lengths = self.data_loader.get_batch(self.hps.batch_size)
            z = Variable(torch.zeros((self.hps.batch_size, self.hps.latent_vector_length)).cuda().float())
            z_stack = torch.stack([z] * (self.N_max + 1))
            inputs = torch.cat([batch, z_stack], 2)

            optimizer_G.zero_grad()
            pi, mu_x, mu_y, sigma_x, sigma_y, rho_xy, q, _, _ = self.generator(inputs, z, len_out=self.N_max + 1)
            generated_sequences = utils.generate_sequences_from_distributions(pi, mu_x, mu_y, sigma_x, sigma_y, rho_xy,
                                                                              q)

            # Generator Loss
            # Adversarial loss

            gen_validity = self.discriminator(generated_sequences, self.hps.batch_size)
            loss_GAN = criterion_GAN(gen_validity, valid)

            # Content loss
            mask, delta_x, delta_y, pen_state = self.get_ground_truth(batch, lengths)
            loss_content, Ls, Lp = self.compute_reconstruction_loss(mask, delta_x, delta_y, pen_state, pi, mu_x, mu_y,
                                                                    sigma_x, sigma_y,
                                                                    rho_xy,
                                                                    q)

            # Total loss
            loss_G = (1 - self.hps.gan_loss_weight) * loss_content + self.hps.gan_loss_weight * loss_GAN

            loss_G.backward()
            optimizer_G.step()

            # Discriminator Loss
            # Los of real and fake sketches
            optimizer_D.zero_grad()

            loss_real = criterion_GAN(self.discriminator(batch, self.hps.batch_size), valid)
            loss_fake = criterion_GAN(self.discriminator(generated_sequences, self.hps.batch_size), fake)

            # Total loss
            loss_D = (loss_real + loss_fake) / 2

            loss_D.backward()
            optimizer_D.step()

            print('epoch', i, 'generator loss', loss_G.data[0], 'discriminator loss', loss_D.data[0])
            if (i % 100 == 0):
                torch.save(self.generator, self.hps.model_path.format(self.dataset_name))
                utils.generate_image_with_model(self.N_max, self.generator, i, self.dataset_name)


if __name__ == '__main__':
    dataset_dir = "dataset/"
    dataset_list = [f for f in listdir(dataset_dir) if isfile(join(dataset_dir, f))]
    for dataset_name in dataset_list:
        model = Model(dataset_name[:-4])
        model.train()
